import argparse
import datetime
import functools
import json
import os
import random
from typing import Optional, Sequence, Tuple, Type

import numpy as np
import wandb

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.state_dict import get_state_dict, set_state_dict
from torch.distributed.fsdp import (
    BackwardPrefetch,
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    ShardingStrategy,
)
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy
import torch.nn as nn
from torch.optim import Adam
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, Subset

from sequence_models.samplers import (
    SortishSampler,
    ApproxBatchSampler,
    ClusteredSortishSampler,
)
from sequence_models.utils import transformer_lr

from evodiff.utils import Tokenizer
from dayhoff.activation_checkpointing import apply_activation_checkpointing
from dayhoff.collators import LMCollator, OAMaskCollator
from dayhoff.constants import MSA_ALPHABET_PLUS, TaskType
from dayhoff.datasets import UniRefDataset
from dayhoff.model import (
    ARDiffusionModel,
    OrderAgnosticDiffusionModel,
    OTHER_METRICS_KEY,
)
from dayhoff.model import create_model
from dayhoff.utils import load_checkpoint, seed_everything


# default to a single-GPU setup if not present
RANK = int(os.environ["RANK"])
LOCAL_RANK = int(os.environ["LOCAL_RANK"])
WORLD_SIZE = int(os.environ["WORLD_SIZE"])
DEVICE = torch.device(f"cuda:{LOCAL_RANK}")


def is_amlt() -> bool:
    return os.environ.get("AMLT_OUTPUT_DIR", None) is not None


def load_config_and_model(
    config_fpath: str,
) -> Tuple[dict, Tokenizer, nn.Module, Type[nn.Module]]:
    """Parses the experiment config to load the model and tokenizer

    Parameters:
    -----------
    config_fpath: str
        The path to the experiment config file

    Returns:
    --------
    config: dict
        The experiment config
    tokenizer: Tokenizer
        The model's tokenizer
    model: nn.Module
        A task-wrapped version of the specified model, which returns the appropriate loss and metrics
    block: Type[nn.Module]
        The block class used repeatedly in the module. It should not be split by any sharding.
    """
    with open(config_fpath, "r") as f:
        config = json.load(f)
    config["task"] = config["task"].lower().strip()
    tokenizer = Tokenizer(MSA_ALPHABET_PLUS)
    task = TaskType(config["task"].lower().strip())

    # create the model
    model, block = create_model(
        task, config["model_type"], config["model_config"], tokenizer.mask_id.item()
    )

    # add the task-specific wrapper
    aux_loss_weight = config.get("aux_loss_weight", 0.0)
    if task == TaskType.OADM:
        model = OrderAgnosticDiffusionModel(
            model, tokenizer.pad_id, aux_loss_weight=aux_loss_weight
        )
    elif task == TaskType.LM:
        model = ARDiffusionModel(model, aux_loss_weight=aux_loss_weight)
    else:
        raise ValueError(f"Unknown task: {config['task']}")
    return config, tokenizer, model, block


def get_dataloader(
    config: dict, tokenizer: Tokenizer, args: argparse.Namespace
) -> DataLoader:
    if is_amlt():
        data_top_dir = args.data_root or "/ddn/evodiff/"
    else:
        data_top_dir = args.data_root or "/data1/data/"

    dataset = config["dataset"]
    data_dir = os.path.join(data_top_dir, dataset + "/")

    if config["task"] == "oadm":
        collator = OAMaskCollator(
            tokenizer=tokenizer,
            pad_to_multiple_of=config.get("pad_to_multiple_of", None),
        )
    elif config["task"] == "lm":
        collator = LMCollator(
            tokenizer=tokenizer,
            pad_to_multiple_of=config.get("pad_to_multiple_of", None),
            flip_prob=config.get("flip_prob", 0.0),
            fim_prob=config.get("fim_prob", 0.0),
            swap_bos_eos_on_flip=config.get("swap_bos_eos_on_flip", True),
        )
    else:
        raise ValueError(f"Unknown task: {config['task']}")

    # load the dataset
    ds_train = UniRefDataset(data_dir, "train", max_len=config["max_len"])
    train_idx = ds_train.indices

    # create the dataloader
    if args.mini_run:
        tindices = np.arange(
            0, 1000
        )  # np.arange(21546293,31546293,1)#(1000000,21546293, 1)
        train_indices = np.sort(np.random.choice(tindices, 100, replace=False))
        train_sampler = Subset(ds_train, train_indices)
        len_train = train_indices
        dl_train = DataLoader(
            dataset=train_sampler,
            shuffle=True,
            batch_size=1,
            num_workers=1,
            collate_fn=collator,
        )
    else:
        metadata = np.load(os.path.join(data_dir, "lengths_and_offsets.npz"))
        len_train = np.minimum(metadata["ells"][train_idx], config["max_len"])
        if "uniref50" in dataset:
            train_sortish_sampler = SortishSampler(
                len_train, config["bucket_size"], num_replicas=WORLD_SIZE, rank=RANK
            )
        elif "uniref90" in dataset:
            with open(os.path.join(data_dir) + "clustered_splits.json") as f:
                clusters = json.load(f)["train"]
            train_sortish_sampler = ClusteredSortishSampler(
                len_train,
                clusters,
                config["bucket_size"],
                num_replicas=WORLD_SIZE,
                rank=RANK,
            )
        train_sampler = ApproxBatchSampler(
            train_sortish_sampler,
            config["max_tokens"],
            config["max_batch_size"],
            len_train,
            batch_mult=8,
        )

        dl_train = DataLoader(
            dataset=ds_train,
            batch_sampler=train_sampler,
            num_workers=8,
            collate_fn=collator,
            pin_memory=True,
        )

    return dl_train


def step(
    model: nn.Module,
    batch: Sequence[torch.Tensor],
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler._LRScheduler,
) -> dict:
    if any(el.numel() for el in batch) == 0:
        raise ValueError("Empty tensor in batch")

    batch = [el.to(DEVICE) for el in batch]
    # step through model
    optimizer.zero_grad()
    outputs = model(*batch)
    outputs["loss"].backward()
    optimizer.step()
    scheduler.step()
    return outputs


def save_checkpoint(
    out_dir: str,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler._LRScheduler,
    step: int,
    epoch: int,
    tokens: int,
    sequences: int,
) -> None:
    out_path = os.path.join(out_dir, f"dcp_{step}")
    print(f"Saving checkpoint to {out_path}", RANK, flush=True)
    model_state, optim_state = get_state_dict(model, optimizer)
    sd = {
        "model_state_dict": model_state,
        "optimizer_state_dict": optim_state,
    }
    fs_storage_writer = torch.distributed.checkpoint.FileSystemWriter(out_path)
    _ = dcp.save(sd, storage_writer=fs_storage_writer)
    if RANK == 0:
        sched_state = scheduler.state_dict()
        sd = {
            "step": step,
            "tokens": tokens,
            "sequences": sequences,
            "scheduler_state_dict": sched_state,
            "epoch": epoch,
        }
        torch.save(sd, os.path.join(out_path, "scheduler.pt"))


def epoch(
    model: nn.Module,
    dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler._LRScheduler,
    args: argparse.Namespace,
    current_epoch: int,
    current_step: int,
    current_tokens: int,
    current_sequences: int,
) -> Tuple[int, int, int]:
    print("Starting epoch", flush=True)
    model = model.train()
    print("Model set to train", flush=True)
    total_steps = current_step
    total_tokens = current_tokens
    total_seq = current_sequences

    for batch in dataloader:
        if args.verbose:
            print("rank", RANK, "batchsize", batch[0].shape, flush=True)

        # def step(model, batch, device, optimizer, scheduler) -> dict:
        output = step(model, batch, optimizer, scheduler)

        # Accurate metric logging with reduce
        # Log number of sequences and processed tokens in one operation
        with torch.no_grad():
            reduce_tensor = torch.stack((output["n_processed"], output["n_seqs"]))
            dist.reduce(reduce_tensor, 0, op=dist.ReduceOp.SUM)

        total_steps += 1
        total_tokens += int(reduce_tensor[0].item())
        total_seq += int(reduce_tensor[1].item())

        if RANK == 0:
            # log metrics to wandb
            wandb.log(
                {
                    "loss": output["loss"].item(),
                    "nsteps": total_steps,
                    "epoch": current_epoch,
                    "token_trained": total_tokens,
                    "sequences_trained": total_seq,
                    "lr": optimizer.param_groups[0]["lr"],
                    **{k: v.item() for k, v in output[OTHER_METRICS_KEY].items()},
                }
            )

        if total_steps % args.checkpoint_freq == 0:
            save_checkpoint(
                args.out_fpath,
                model=model,
                optimizer=optimizer,
                scheduler=scheduler,
                step=total_steps,
                epoch=current_epoch,
                tokens=total_tokens,
                sequences=total_seq,
            )

    return total_steps, total_tokens, total_seq


def train(args: argparse.Namespace) -> None:
    print(
        f"Starting job on rank {RANK} with local rank {LOCAL_RANK} and world size {WORLD_SIZE}"
    )
    seed_everything(args.random_seed)

    dist.init_process_group(backend="nccl")
    # get the config, tokenizer, and model
    if args.verbose:
        print("Initializing model...", RANK)
    config, tokenizer, model, blk_types = load_config_and_model(args.config_fpath)
    if RANK == 0:
        if args.no_wandb:
            wandbmode = "disabled"
        else:
            wandbmode = "online"
        wandb.init(config=config, mode=wandbmode)
    if args.verbose:
        print("Done initializing model.", RANK)

    # store the command line args in the config and dump to disk
    config["dtype"] = args.dtype
    config["random_seed"] = args.random_seed
    config["world_size"] = WORLD_SIZE
    if RANK == 0:
        os.makedirs(args.out_fpath, exist_ok=True)
        with open(os.path.join(args.out_fpath, "config.json"), "w") as f:
            json.dump(config, f)

    # training dtype and local device
    dtype = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }[args.dtype]

    padding_idx = tokenizer.pad_id  # PROTEIN_ALPHABET.index(PAD)
    if RANK == 0:
        print("Using {} as padding index".format(padding_idx))
        print("Using {} as masking index".format(tokenizer.mask_id))
        print(
            f"Model has {sum(p.numel() for p in model.parameters())} trainable parameters."
        )
    if args.verbose:
        print("Moving and sharding model...", RANK)
    # set the default device
    torch.cuda.set_device(LOCAL_RANK)

    # setup FSDP
    # don't split ByteNetBlock's across devices
    wrap_policy = functools.partial(
        transformer_auto_wrap_policy, transformer_layer_cls=blk_types
    )
    mixed_precision = MixedPrecision(param_dtype=dtype, buffer_dtype=dtype)
    shard_strategy = ShardingStrategy._HYBRID_SHARD_ZERO2
    bwd_prefetch = BackwardPrefetch.BACKWARD_PRE
    model = FSDP(
        model,
        device_id=DEVICE,
        auto_wrap_policy=wrap_policy,
        sharding_strategy=shard_strategy,
        mixed_precision=mixed_precision,
        backward_prefetch=bwd_prefetch,
    )
    # create the optimizer and scheduler
    epochs = config["epochs"]
    lr = config["lr"]
    warmup_steps = config["warmup_steps"]
    optimizer = Adam(
        model.parameters(), lr=lr, weight_decay=config.get("weight_decay", 0.0)
    )
    lr_func = transformer_lr(warmup_steps)
    scheduler = LambdaLR(optimizer, lr_func)

    # load the state
    initial_epoch, total_steps, total_tokens, total_seqs = load_checkpoint(
        model, optimizer, scheduler, args.out_fpath, args.last_step
    )
    # override from config
    optimizer.param_groups[0]["lr"] = config["lr"] * lr_func(total_steps + 1)
    optimizer.param_groups[0]["initial_lr"] = config["lr"]
    scheduler.base_lrs = [config["lr"]]
    act_ckpt = config.get("activation_checkpointing", None)
    if act_ckpt is not None:
        apply_activation_checkpointing(model, blk_types, act_ckpt)

    if args.verbose:
        print("Initializing data...", RANK)
    dl_train = get_dataloader(config, tokenizer, args)
    if args.verbose:
        print("Done initializing data.", RANK)
    if RANK == 0:
        print(f"Training on {len(dl_train.dataset)} sequences.")
    # train
    for e in range(initial_epoch, epochs):
        start_time = datetime.datetime.now()
        if not args.mini_run:
            if args.verbose:
                print(RANK, "Setting epoch")
            dl_train.batch_sampler.sampler.set_epoch(e + 1)
        print('Epoch set', flush=True)
        total_steps, total_tokens, total_seqs = epoch(
            model,
            dl_train,
            optimizer,
            scheduler,
            args,
            current_epoch=e,
            current_step=total_steps,
            current_tokens=total_tokens,
            current_sequences=total_seqs,
        )

        save_checkpoint(
            args.out_fpath,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            step=total_steps,
            epoch=e,
            tokens=total_tokens,
            sequences=total_seqs,
        )
        print(f"Epoch {e} complete in {datetime.datetime.now() - start_time}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_fpath")
    parser.add_argument(
        "out_fpath",
        type=str,
        nargs="?",
        default=os.getenv("AMLT_OUTPUT_DIR", "/tmp") + "/",
    )
    parser.add_argument("data_root", type=str, nargs="?", default=None)
    parser.add_argument(
        "--mini_run", action="store_true"
    )  # Set to True if running on subset of data
    parser.add_argument("--checkpoint_freq", type=int, default=2000)  # in steps
    parser.add_argument(
        "--random_seed", type=int, default=0
    )  # lambda reweighting term from Austin D3PM
    parser.add_argument("--dtype", type=str, default="bfloat16")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no_wandb", action="store_true")
    parser.add_argument("--last_step", default=-1, type=int)
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
