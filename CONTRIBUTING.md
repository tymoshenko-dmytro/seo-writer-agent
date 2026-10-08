# Contributing

Thank you for helping. Issues and pull requests are welcome.

## Set up

Python 3.14.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium
.venv/bin/python -m pytest
```

The tests read no key and reach no network (`tests/netguard` refuses any connection off the
machine). `tests/standin.py` runs a whole article on stand-in services: when you change what a
step asks a model or what it returns, change its canned answer there too.

`python bin/docker_smoke.py` builds the image and checks it the way a new user meets it (it needs
Docker). CI runs the tests, the Docker smoke and a secret scan on every pull request.

## Pull requests

- One change per pull request, with a test that fails without it.
- No keys, no real articles, no customer data in the repository: test data is made up.
- The prompts (`core/prompts/`) stay general. Anything true of one product or one industry belongs
  in a project's texts (brand voice, product facts, editorial rules), not in a prompt.
- A change to what an article folder holds: run `python bin/make_sample.py` and commit the new
  `samples/sample-article/`.
- Dependencies: change a range in `requirements.in`, install into a clean venv, run the tests,
  then `pip freeze --exclude-editable > requirements.txt`.

## Writing

Interface texts, prompts and documentation are written in plain English: short sentences, the
same word for the same thing, no marketing. A message that refuses something says what was not
done and why.
