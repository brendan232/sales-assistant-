# Sales assistant

A proactive assistant for one account executive. It reads HubSpot, Fathom call recordings, Gmail and
Google Calendar, then tells you which deals are slipping, who else you should be talking to in each
account, and how to win, with the follow-up emails already written.

It never sends email (drafts only) and asks before writing to HubSpot.

## Quick start (sample data, no accounts needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env   # add ANTHROPIC_API_KEY; leave the rest blank to use sample data
```

```bash
.venv/bin/python -m sales_assistant web
```

This opens the interface in your browser: your open deals ranked by risk on the left, chat on the right.
Anything the assistant wants to write (an email draft, a CRM note or task) appears as a card with
Approve and Decline buttons. It runs on your own computer only (http://127.0.0.1:8765).

## Commands

| Command | What it does |
|---|---|
| `python -m sales_assistant web` | Web interface. `--port` to change the port, `--no-browser` to not open a window. |
| `python -m sales_assistant chat` | The same assistant in the terminal. Write actions prompt for approval (`--yes` to skip). |
| `python -m sales_assistant brief` | Unattended daily brief, printed and saved to `briefs/YYYY-MM-DD.md`. No writes; add `--create-drafts` to also save follow-ups as Gmail drafts. |
| `python -m sales_assistant deal "Northwind"` | Full account plan for one deal. |
| `python -m sales_assistant doctor` | Checks each connector with one read. No model calls. |

Flags: `--effort low|medium|high|xhigh|max` (default `high`), `--no-web` to turn off web search.

To make it proactive, schedule the brief, e.g. weekdays at 07:40:

```
40 7 * * 1-5  cd /path/to/sales-assistant && .venv/bin/python -m sales_assistant brief --create-drafts
```

## Connecting your systems

Each connector falls back to sample data until its credentials are set in `.env`. Run `doctor` after each.

- **HubSpot**: create a service key (Development > Keys > Service keys) with read scopes for deals,
  contacts and companies, plus write on deals/contacts if you want notes and tasks. Put the token in
  `HUBSPOT_ACCESS_TOKEN`. Set `HUBSPOT_OWNER_ID` to limit it to your own deals.
- **Fathom**: put your API key in `FATHOM_API_KEY`. This connector was written without access to
  Fathom's API reference, so it is the one most likely to need a field-name fix on first run.
- **Gmail + Calendar**: create an OAuth "Desktop app" client in Google Cloud with the Gmail and Calendar
  APIs enabled, save it as `credentials.json` here. First run opens a browser for consent. Scopes are
  read mail, create drafts, read calendar.

Then fill in `playbook.md` (product, buying committee, pricing guardrails, competitors, your email voice).
It is loaded into the assistant's instructions and is the biggest lever on output quality.

## How it works

- `sales_assistant/agent.py`: Claude (`claude-opus-5-5`) driven by the Anthropic SDK tool runner. The
  conversation history is kept locally so follow-up questions build on earlier ones.
- `sales_assistant/tools.py`: the tools the model can call. Reads run freely; drafts, notes and tasks go
  through `Approver` (ask / allow / deny).
- `sales_assistant/signals.py`: risk flags (stalled, single-threaded, no senior contact, close date
  passed, no next step, no meeting booked) computed in code so the brief is consistent.
- `sales_assistant/connectors/`: HubSpot, Fathom, Google, and sample-data stand-ins with the same methods.
- `sales_assistant/prompts.py`: the system prompt and the daily-brief prompt.
- `sales_assistant/web.py` and `static/index.html`: the local web interface.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Offline: sample-data tools, the approval gate, and the agent loop against a stubbed Claude API.
