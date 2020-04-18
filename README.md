# Leetcode-Crawler
Crawl your accepted solutions on LeetCode and push them to a git repository.

## Installation
1. Install [pipenv](https://github.com/pypa/pipenv)
2. Use `pipenv install` to install all dependencies
3. Copy [config-sample.ini](config-sample.ini) to `config.ini`
4. Fill in your LeetCode username and password to `config.ini`
5. Use `git init submissions` or `git clone [your_remote_repo] submissions` to create the `submissions` folder

## Usage
Just execute [main.py](main.py). It will use [Chromium](https://www.chromium.org/) to crawl your accepted solutions since the last updated time set in `config.ini` .
