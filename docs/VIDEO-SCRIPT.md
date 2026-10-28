# Video script: one door for every model call (LLM gateway)

**Title:** One door for every model call: the LLM gateway in six minutes

**Length:** about 5 minutes 55 seconds, spoken by local neural voices (British, alternating by topic; see the section headers of video/narration.txt)

**Audience:** customers evaluating EDB AgentPG sample applications, and internal people (solutions engineers, platform and demo owners) who
need to show or run the gateway. No prior knowledge of the gateway is assumed. Basic familiarity with "an app that calls an AI model" is.

**Goal:** after watching, the viewer can explain what the gateway is for, has seen an app get its own key, has seen one alias fail over to a backup, has
seen a budget stop an app, and knows where to read more. The video shows that switching an entire fleet to another model is a one-field edit.

**Companion documents:** [GUIDE.md](GUIDE.md) for every detail, [../API.md](../API.md) for endpoints, [../README.md](../README.md) for the short tour.

---

## Prerequisites and seed data (reproducible locally)

Nothing here needs a real model provider, a cloud account or a live URL. Everything runs on one laptop with Docker, Python 3.11+ and `uv`.
Record on a fresh database so the first-run screen appears.

**Tools:** Docker, `uv`, `curl`, `jq`, a browser at 1280x720 or larger, a terminal with a large font (at least 18 pt).

**A. Start the gateway** (from the `llm-gateway` directory, with the sibling `../agentpg-kit` checkout present):

```bash
cp .env.example .env            # set POSTGRES_PASSWORD to any throwaway value
docker compose down -v          # make sure the database is empty
docker compose up -d db
uv venv && uv pip install -e ".[dev]"
set -a; . ./.env; set +a
export LLMGW_ALLOW_PRIVATE_URLS=true     # only so the local fake provider in step B is accepted
.venv/bin/llmgw migrate
.venv/bin/llmgw serve                    # leave running; the one-time setup code is printed here
```

Keep this terminal visible for the setup code in scene 3. The code changes on every restart, so do not restart the server between the dry run and the take.

**B. A fake "flaky" provider** (so the failover scene needs no real provider). Save this as a file outside the repository, for example
`/tmp/flaky.py`, and run `python3 /tmp/flaky.py` in a second terminal:

```python
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(503)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "overloaded"}')

HTTPServer(("127.0.0.1", 9099), H).serve_forever()
```

It answers every request with HTTP 503, which is exactly what makes the gateway move to the next target.

**C. Before you press record**, do the setup once in a throwaway run only to test the commands, then reset (`docker compose down -v`, restart steps A and B) so the
real take starts at the setup screen. Nothing below needs to exist before scene 3 except the two terminals.

**Variables used by the commands on screen** (type these off camera in the third terminal, the "demo" terminal, so the lines stay short):

```bash
export GW=http://localhost:8000
# filled in during the recording:  APP_KEY=lg_...   (the trustdesk key from scene 4)
#                                  LOW_KEY=lg_...   (the budget-demo key from scene 8)
#                                  OPS=lgt_...      (a read-scope operator token, created off camera at the start of scene 9)
```

**Rehearsal status:** the whole sequence below was rehearsed end to end against a local gateway with `docs/video/record.spec.ts` (see Recording notes). Verified for real: setup flow, `trustdesk` key and echo reply (`Echo from echo-1: hello`, model `echo-chat`), 768-number embedding, the `flaky` Test result (`Failed (..ms): http_5xx: {"error": "overloaded"}`), failover (`attempts: 2`, provider `echo`), the 40-token hard budget (200, 200, 429, 429, 429, 429; `budget_exceeded`, a `Retry-After` of several days' worth of seconds), a 2 requests-per-minute client (200, 200, 429, 429), the red overview banner and the audit rows. The Docker steps in A were not run (the rehearsal used a local throwaway Postgres instead of the compose database).

**Names that must match the script:** clients `trustdesk` and `budget-demo`; providers `flaky` (base URL `http://127.0.0.1:9099/v1`, key any placeholder text such as
`not-a-real-key`, which the table shows as `…-key`) and the built-in `echo`; alias `resilient-chat`.

---

## Scenes

The NARRATION column is a mirror of `video/narration.txt` (the text written for listening; `video/build.py` regenerates this column on every build, so edit narration.txt, not the table). `[pause 1.5]` marks a deliberate silence while something happens on screen. Times are approximate.

| # | Time | ON-SCREEN ACTION | NARRATION | CALLOUTS |
|---|---|---|---|---|
| 1 | 0:00 - 0:25 | Title card, then the browser at the gateway's address showing the empty first-run screen. | "Say you run several apps that use AI models. Each one holds its own provider key. Each one decides which model to call. This is the LLM gateway. One place, in front of your model providers, that every app calls instead. In this video you will see three things. An app getting its own key. A failing provider being handled. And a budget stopping runaway cost." | Text: "LLM gateway" and "one OpenAI-compatible endpoint, on Postgres" |
| 2 | 0:25 - 0:51 | Full-screen diagram: apps (TrustDesk, PagerLens, PayBroker) on the left, the gateway in the middle, providers on the right. Use the mermaid diagram in GUIDE.md section 2 or draw three boxes. Highlight the arrow labelled "lg_ key", then the arrow labelled "provider key". | "Here is the idea. An app sends a normal OpenAI style request to the gateway. It carries a key that belongs only to that app. The gateway checks that key, the app's limits, and which models it may use. Then it calls the real provider, with the real provider key. That key never leaves the gateway. Because apps speak the standard OpenAI format, they need no code change. Only a new address, and a new key." | Callout 1: "App key: revocable, budgeted". Callout 2: "Provider key: encrypted, stored once" |
| 3 | 0:51 - 1:24 | Switch to the terminal running `llmgw serve`; point at the setup code block. Back in the browser: type the code into **Setup code**, type a 12+ character password in both password fields, submit. The Overview page appears with "No traffic yet". | "Now, getting set up: on first start, the gateway prints a one-time setup code in its log. Whoever holds that code chooses the administrator password. That password also protects the vault, where provider keys are encrypted. So the database alone reveals nothing. The gateway starts with a built-in demo provider called echo. It needs no network and no key. So you can try everything without paying for a single token." | Callout on the log: "One-time setup code". Callout on the page: "Seeded: echo provider, echo-chat, echo-embed, chat-default, embed-default" |
| 4 | 1:24 - 2:07 | Click **Clients** in the top navigation. Click **New client**. Type name `trustdesk`, leave "All models", set Requests per minute to `60`, leave the budget empty, click **Create client and issue a key**. Click **Copy** next to the key (a "Key copied" toast appears; the key itself is blurred on screen). In the demo terminal the copied key is held in `$APP_KEY`; run `curl -s $GW/v1/chat/completions -H "Authorization: Bearer $APP_KEY" -H "Content-Type: application/json" -d '{"model":"echo-chat","messages":[{"role":"user","content":"hello"}]}' \| jq '.choices[0].message.content, .model, .usage'`. | "Every app gets its own client. I will call this one trustdesk, after the app. I allow all models, and sixty requests a minute. When I create it, the gateway shows the key once. Only a hash is stored, so a lost key can only be replaced. I copy it. Now I call the model with plain curl, using that key. [pause 1.5] The reply is the echo of my words. Notice two things. The model name is the alias I asked for, not the provider's internal name. And there is a usage block with token counts. That is what the gateway meters." | Callout on key panel: "Shown once. Only a SHA-256 hash is stored." Callout on output: "model = alias" and "usage" |
| 5 | 2:07 - 2:33 | Demo terminal: `curl -s $GW/v1/models -H "Authorization: Bearer $APP_KEY" \| jq '.data[].id'`, then `curl -s $GW/v1/embeddings -H "Authorization: Bearer $APP_KEY" -H "Content-Type: application/json" -d '{"model":"echo-embed","input":["first text"]}' \| jq '.data[0].embedding \| length'`. | "The app can list the models it may use. And it can ask for embeddings. Embeddings are the number lists used for search. The demo returns a vector of seven hundred sixty-eight numbers. The same text always gives the same numbers. Chat and embeddings are what the gateway serves. It does not stream answers, and it does not forward tool calls." | Callout on the number: "768 numbers". Text: "Chat and embeddings. No streaming." |
| 6 | 2:33 - 3:07 | Browser: **Providers** > **Add a provider**. Choose "Other OpenAI-compatible endpoint" (or leave the preset), name `flaky`, base URL `http://127.0.0.1:9099/v1`, API key `not-a-real-key`, save. On the new row click **Test**, upstream model `flaky-1`, **Test**; the result shows "Failed (about 70 ms): http_5xx: {\"error\": \"overloaded\"}". Then **Models** > **New model alias**: alias `resilient-chat`, kind chat, target 1 provider `flaky` model `flaky-1`, **Add a fallback target**, target 2 provider `echo` model `echo-1`, **Save**. | "Now providers and failover, starting with one that is down. I add a provider, with its address and key. The key goes into the encrypted vault. Only its last four characters are ever shown again. Now I press Test, and the gateway makes one tiny call. [pause 1.5] It fails. This is my stand-in for an outage. Next, a model alias. Its first target is the failing provider. Its second target is the echo provider. Targets are tried in order." | Callout on key column: "Only the last 4 characters are shown". Callout on alias editor: "Ordered targets: 1 to 5". Text on Test result: "Simulated outage" |
| 7 | 3:07 - 3:37 | Demo terminal: call `resilient-chat` with `$APP_KEY` (same curl as scene 4 with `"model":"resilient-chat"`). It succeeds. Then show the proof: `curl -s $GW/api/usage/recent?limit=1 -H "Authorization: Bearer $OPS" \| jq '.items[0] \| {alias, provider_name, status, attempts}'` showing `attempts: 2` and provider `echo`. | "Now I call the new alias. [pause 1.5] The app gets a good answer, and never knew anything went wrong. The usage record shows what happened. Two attempts, and echo was the provider that answered. The gateway moves to the next target after a server error, a rate limit, or a timeout. It also moves on after a connection problem. It does not move on after other client errors. A different provider would not fix a bad request." | Callout on JSON: "attempts: 2, provider: echo". Text: "Failover on 5xx, 429, timeout, connection error" |
| 8 | 3:37 - 4:20 | Browser: **Clients** > **New client**: name `budget-demo`, Monthly token budget `40`, When the budget is used up: "Hard: refuse requests once used up", create, copy the key. Demo terminal: `export LOW_KEY=lg_...`, then `for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w "%{http_code}\n" $GW/v1/chat/completions -H "Authorization: Bearer $LOW_KEY" -H "Content-Type: application/json" -d '{"model":"echo-chat","messages":[{"role":"user","content":"spend some tokens please"}]}'; done`. The output turns from 200 to 429. Then show the body once: same call without `-o /dev/null`, piped to `jq .error.code`, giving `budget_exceeded`. | "Now, limits and budgets. I create a second client, with a tiny budget. Forty tokens a month, in hard mode. Hard means requests are refused once the budget is gone. Now I send six requests. [pause 2] The first two get a 200. From the third request on, the answer is 429. The error code is budget exceeded. And the response says how many seconds remain until the next month. A soft budget would let requests through, and flag them instead. The count is checked before each call, so one request can slightly overshoot." | Callout on terminal: "200, 200, 429, 429, 429, 429" (verified: the third request is the first refusal). Callout on error: "budget_exceeded + Retry-After". Text: "Hard = refuse. Soft = allow and flag." |
| 9 | 4:20 - 4:52 | Browser: **Overview** (the red "Budget used up: budget-demo" banner is visible), then scroll through the clients table (spend against budget), models and errors. Click **Audit** and scroll the list of changes (provider added and tested, alias created, clients created, keys issued, plus a `token.create` row from the operator token made off camera). | "Back in the dashboard, the overview shows requests per day. Spend for each app, against its budget. The busiest models, and errors by type. The red banner names the client that hit its hard budget. The audit page records who changed what, and when. Providers, aliases, clients, and keys. It records that a key was issued, never the key itself. And no prompts or answers are stored anywhere. Only counts, names, timings, and status." | Callout on banner: "Budget used up: budget-demo". Callout on audit: "Who, what, when. Never secrets." Text: "No prompts or completions stored" |
| 10 | 4:52 - 5:25 | Browser: **Models**, click **Edit** on the `chat-default` row (the window is titled "Edit chat-default"; do not save; just show target 1 and close the window with the X). Then **Connect**: scroll to the account fields and the curl snippets. | "Now the part that matters at fleet scale. Every app in the ecosystem is wired to two default aliases. Chat-default, and embed-default. Today they point at the demo provider. To move every app to a real model, add the provider. Then change the target of that alias. No app, no client, and no key changes. The Connect page gives each app its setup, ready to copy. With single sign-on, administrators can do everything. Operators manage clients and models. Viewers can only read usage." | Callout on `chat-default` target: "Change this one field to move the whole fleet". Text: "Roles: admin, operator, viewer" |
| 11 | 5:25 - 5:54 | Title card with three lines: the repository's `docs/GUIDE.md`, `API.md`, and the sentence "Add a provider, repoint chat-default, done". | "To recap. One gateway holds your provider keys. Each app gets its own key, limits, and budget. A failing provider fails over to the next target. Usage is metered per app, without storing prompts. And one alias edit switches the whole fleet. To try it yourself, start the gateway locally with Docker. Sign in with the setup code, and use the built-in echo models. The full guide is in the docs folder of the project. Thanks for watching." | Text: "docs/GUIDE.md", "API.md". End card: "Try it: docker compose up --build" |

If the take runs long, cut the last two sentences of scene 8 and the roles sentences of scene 10 from narration.txt.

---

## Recap and call to action (for the end card and the video description)

- Recap: one place for provider keys; one key, limit and budget per app; automatic failover; per-app usage with no stored prompts; one alias edit moves the fleet.
- Call to action: run it locally (`docker compose up --build`, read the setup code with `docker compose logs api`), then read `docs/GUIDE.md` for the ecosystem wiring (TrustDesk, PagerLens, PayBroker, ShopFront, the simulator, identity and `ecoctl`). Do not put a live URL in the video or its description; URLs change when a sandbox is recreated.

---

## Recording notes

### General

- Resolution 1280x720 or 1920x1080; browser zoom 110 to 125 percent; dark or light theme, but not switching during the take.
- Blur or crop the keys in the clients panel after the take, and use throwaway values only (they die with `docker compose down -v`). The setup code is also throwaway.
- Pre-type the long curl commands as shell history entries or snippets so the camera never waits on typing; show them being run, not typed.
- Narrate over the actions; leave one second of silence at each scene change for cutting.

### The Playwright recorder

`docs/video/record.spec.ts` performs every on-screen browser action of the scenes above in real Chrome and asserts the numbers. It starts its own throwaway Postgres
(database name ends in `test`, 127.0.0.1 only, no unix socket), the gateway from `../../.venv` on a free port with `LLMGW_ALLOW_PRIVATE_URLS=true`, and the fake always-503 provider, so nothing live is touched. The "terminal" scenes are drawn as an on-screen
terminal panel showing the real command and the real response (the curl calls are made with `fetch`); callouts are yellow label bars with an outline around the element.

```bash
cd docs/video
npm install                         # @playwright/test; needs Google Chrome installed
npx playwright install ffmpeg       # one-time: Playwright's video encoder
npx playwright test                 # 1920x1080, slow human pacing, video in docs/video/out/ (git-ignored)
PACE=0.05 NOVIDEO=1 npx playwright test   # fast rehearsal with all assertions, no video (SHOTS=/dir saves a PNG per callout)
```

It reuses the process and Postgres helpers of the sibling `ecosystem/e2e/harness` (override the location with `E2E_HARNESS`), and needs `PG_BIN` there to point at a PostgreSQL 17 with pgvector.
Narration and the final cut are separate work; the output is silent video.

Facts that differ from a first reading of the source (now verified):

- The dashboard sends a strict content-security policy: inline `style` attributes in injected markup are ignored, so overlays must set styles through the CSSOM (the recorder does).
- Setup button: **Create admin password**. A model row opens with **Edit** (not by clicking the row). Provider dropdown entry for the built-in provider: `echo (built-in)`.
- The client-key window appears twice in a row (trustdesk, then budget-demo) in the same `#detail` dialog: wait for the client name in `#detail-body` before reading the key, or you may copy the previous key.
- Scene 8 budget: with the sample message each request meters about 20 tokens; 40 tokens trips on the third request (observed 200, 200, 429, ...).
- Rate limits: a client with `rpm_limit` 2 answers 200, 200, 429, 429 within a minute (error code `rate_limit_exceeded`; shows in the overview's Errors table).

### Checks before publishing

- Re-run the full sequence on a clean database at least once; the numbers in scene 8 depend on echo's token estimate (characters divided by four, rounded up, plus a few per message). Observed: a budget of 40 trips on the third request.
  If the echo estimate ever changes, re-run the recorder and adjust the budget or the message.
- Confirm that the Test button on `flaky` shows "Failed" (verified in the rehearsal; the fake provider must be running and `LLMGW_ALLOW_PRIVATE_URLS=true` must be exported in the shell that started the server).
- Make sure no real key, email address, sandbox name or live URL is visible in any frame.
