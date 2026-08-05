from setuptools import setup

setup(
    name='vast_cli',
    version='0.1.1',
    packages=[
        "vast_cli",
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
    url='https://github.com/2xic/vast-ai-api',
)
