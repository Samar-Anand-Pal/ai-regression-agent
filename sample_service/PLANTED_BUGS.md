# Planted bugs (answer key)

The demo server has four deliberate bugs, so the pipeline has real failures to find.
The pipeline never reads this file: it only sees `docs/`, the `.proto` and `client.py`.
Start the server with `--fixed` to switch them all off.

| # | Requirement broken | What the buggy server does |
|---|---|---|
| 1 | REQ-014 | Lets a suspended member check out books |
| 2 | REQ-024 | Charges one day too little for a late return (off by one) |
| 3 | REQ-025 | Allows a third renewal |
| 4 | REQ-032 / REQ-033 | Doesn't set the returned copy aside for the READY hold, so anyone can take it |
