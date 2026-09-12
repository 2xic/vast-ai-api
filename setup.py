from setuptools import setup

setup(
    name="vast_cli",
    version="0.1.2",
    packages=[
        "vast_cli",
        "vast_cli.api",
    ],
    install_requires=[
        "requests",
        "python-dotenv",
    ],
    entry_points={
        "console_scripts": [
            "vast=vast_cli.__main__:main",
        ],
    },
    url="https://github.com/2xic/vast-ai-api",
)
