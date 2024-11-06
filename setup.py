import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="dayhoff",
    version="0.0.0",
    description="Python package for generation of protein sequences and evolutionary alignments via discrete diffusion models",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/microsoft/dayhoff",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    install_requires=[
        "pandas",
        "numpy",
        "sequence-models",
        "scikit-learn",
        "tqdm",
    ],
    python_requires=">=3.8.5",
    include_package_data=True,
    packages=setuptools.find_packages(),
    package_data={"": ["config/*"]},
)
