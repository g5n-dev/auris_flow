# Support

Auris Flow is currently an open-source development baseline. There is no production support SLA.

Use GitHub issues for reproducible bugs, documentation gaps, and local development problems. Do not include secrets, customer data, raw audio, transcripts, private URLs, or credentials in public issues.

For security reports, follow `SECURITY.md` and use a private vulnerability channel.

## Before Opening An Issue

- Run `bash scripts/verify_fast.sh` when possible.
- Include OS, Python, Node, Docker, and browser versions.
- Include the failing command and the relevant `trace_id` if the problem involves BFF or worker behavior.
- State whether the issue was seen with SQLite fast verification or the real MySQL/Redis/MinIO/Qdrant stack.
