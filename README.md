# hg-dumper

A tool to dump a mercurial repository from a website 

## Usage

```
usage: hg-dumper.py [options] URL DIR

Dump a mercurial repository from a website.

positional arguments:
  URL                   url
  DIR                   output directory

optional arguments:
  -h, --help            show this help message and exit
  --proxy PROXY         use the specified proxy
  -j JOBS, --jobs JOBS  number of simultaneous requests
  -r RETRY, --retry RETRY
                        number of request attempts before giving up
  -t TIMEOUT, --timeout TIMEOUT
                        maximum time in seconds before giving up
```

### Example

```
./hg-dumper.py http://website.com/.hg ~/website
```

## Install the dependencies

```
pip install -r requirements.txt
```
