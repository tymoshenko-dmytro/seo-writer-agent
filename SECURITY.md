# Security

## Reporting a problem

Please report a security problem privately, not in a public issue: on this repository's GitHub
page, open **Security > Report a vulnerability**. Say what you found, how to reproduce it, and
what an attacker could do with it.

## What to expect from an install

SEO Writer Agent is built to be self-hosted by one trusted team. Before you put it on a server,
read the limits in the README:

- one password for everyone, no accounts or roles;
- the services' keys are kept on the data volume in a file only its owner can read, not
  encrypted;
- it fetches the pages the research and the article name, with no guard against addresses inside
  a private network;
- the container runs as root, and Chromium with `--no-sandbox`.

Keep it on `127.0.0.1` (the default in `docker-compose.yml`) or behind your own HTTPS proxy with
`UI_COOKIE_SECURE=1`, and never expose it without a password.
