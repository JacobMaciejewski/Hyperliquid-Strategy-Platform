# Design Document — Strategy Plugin Platform

A single host process that runs independently-authored trading strategies as **plugins** against
Hyperliquid testnet. The platform owns the exchange connections, mediates every plugin's access to
market data and order placement, enforces global limits, persists the orders it places, and exposes
one status surface. This document explains what we built and why.

> **To run it yourself:** see [README.md](README.md) for build, run, and the failure-demo commands.
> To write a plugin, see [PLUGIN_GUIDE.md](PLUGIN_GUIDE.md). This document explains the design.

## 1. Architecture

The platform is a thin core that wires a handful of **single-instance services** — each owning one
concern — and runs each plugin on **its own thread fed by a bounded queue**. Everything a plugin
touches goes through one mediated handle (`ctx`), so limits, persistence, and isolation can't be
bypassed.

```
        ┌───────────────────────────────────────────────────────────────────┐
        │                       Hyperliquid testnet                         │
        └───────────────┬─────────────────────────────────────┬─────────────┘
                        │ (1) price feed                      │ (6) orders ↑
                        ▼                                     │ (7) fills  ↓
               ┌────────┬────────┐                            │
               │   Market Data   │                            │
               │     Service     │                            │
               └────────┬────────┘                            │
                        │ (2) push tick into queue            │
                        ▼                                     │
        ┌───────────────┬─────────────────────────┐           │
        │  Plugin Runner — one thread per plugin  │           │
        │   queue ──(3) take one──► plugin        │           │
        └─────────────────────────────┬───────────┘           │
                                      │ (4) ctx.submit_order  │
                                      ▼                       ▼
                            ┌─────────┬─────────┐   ┌─────────┬───────┐
                            │   Order Router    │──►│  Order Manager  │
                            └─┬───────────────┬─┘   │  (mock / live)  │
                              │               │     └─────────────────┘
                              │ (5) check     │ (6) save
                              ▼               ▼
                        ┌─────┬─────┐   ┌─────┬─────┐
                        │   Risk    │   │   Order   │
                        │   Guard   │   │   Store   │
                        └───────────┘   └─────┬─────┘
                                              │ (8) read
                                              ▼
                                        ┌─────┬─────┐
                                        │  Status   │ GET /status
                                        │   page    │
                                        └───────────┘
```

**What the numbers mean** (short version; sections 2–7 go deeper):

1. **Price feed in.** The exchange streams prices over **one** shared websocket. Plugins never open
   their own.
2. **Tick into the queue.** The Market Data Service copies each tick into the inbound queue of every
   plugin that subscribed to it. The queue is bounded — if a plugin falls behind, the oldest tick is
   dropped, not the newest.
3. **Plugin reacts.** Each plugin has its own thread. It takes events off its queue one at a time and
   runs the plugin's code. So a plugin's callbacks never overlap, and a slow one only delays itself.
4. **Plugin places an order.** The plugin's code calls `ctx.submit_order`. `ctx` is the only door out
   — the plugin never touches a service or the exchange directly.
5. **Limits checked.** The router asks the Risk Guard if the order fits the **global** limits. If not,
   the order is rejected right there, with a reason — never silently dropped.
6. **Saved, then sent.** The router saves the order to the Order Store *before* sending it (so a
   restart remembers it), then hands it to the Order Manager, which sends it to the exchange. The
   result (`resting` / `filled` / `rejected`) comes straight back to the plugin — `submit_order` does
   not wait for a fill.
7. **Fills come back later.** When a resting order fills, the exchange tells the Order Manager. The
   platform saves the fill and pushes an update into the owning plugin's queue — so it arrives just
   like a tick (back to step 3).
8. **Status reads the store.** The Status page reads the Order Store to show config, open orders,
   positions, P&L, and errors. It only reads — it never changes state.

Steps **4–6 run on one lock, one order at a time**, so two plugins can't both slip past a limit at
once. Steps 1–3 and 7 are asynchronous, delivered through the queue; steps 4–6 are synchronous and
return immediately.

**The components, one line each:**

| Component | What it owns |
|-----------|------|
| **Platform core** | Reads the config, builds the services once, starts a runner per plugin, runs startup and shutdown. |
| **Plugin Runner** | One plugin's thread and its inbound queue. Catches every error the plugin throws. This is the isolation boundary. |
| **Context (`ctx`)** | The plugin's only handle: `subscribe`, `submit_order`, `cancel`, `log`, `config`. |
| **Market Data Service** | The one shared price websocket: dedupes subscriptions, fans ticks out, reconnects and resubscribes on a drop. |
| **MidPriceTracker** | Caches the latest mid price per coin. Used to fill mock orders and to mark P&L, in both modes. |
| **Order Router** | The submission path. The only part that knows the guard, store, and manager; runs them in order, under one lock. |
| **Risk Guard** | Checks the three global limits before an order is placed. Reads the store; never places or saves. |
| **Order Manager** | Sends orders to the venue — `mock` (simulated fills) or `live` (testnet). Reports every outcome in one shape. |
| **Order Store** | The single source of truth: SQLite on disk plus an in-memory set of open orders. Survives a restart. |
| **Status page** | Read-only `GET /status` (JSON): config, positions, P&L, open orders, market-data health, errors. |

> **Exchange connections are kept minimal:** one shared websocket for prices (all plugins), the HTTP
> API for placing and cancelling, and — in live mode only — one more websocket for fills. Mock mode
> opens neither order connection.

## 2. The plugin contract and rejected alternatives

A plugin is a class. You subclass `Plugin`, override the hooks you need, and keep your state in normal
instance attributes. You only ever talk to the platform through one handle, `ctx`. That is the whole
contract.

```python
class Plugin:
    def on_start(self, ctx):  ...      # once: subscribe, set up state
    def on_market_data(self, event): ...  # each tick of a feed you subscribed to
    def on_order_update(self, update): ...  # a resting order later fills or cancels
    def on_stop(self): ...             # once, on shutdown: cancel or flatten

# ctx: config · subscribe(coin, feed) · submit_order(...) -> result · cancel(cloid) · log(msg)
```

Three choices shaped it.

**Plain function calls, not `async`.** When a tick arrives, the platform calls your `on_market_data`
and waits for it to return. We chose this over `async def` hooks running on a shared event loop. The
trouble with an event loop is that nothing interrupts a coroutine until it pauses with `await`. So if
one plugin loops forever, or just calls something slow by mistake like `time.sleep` or a network
request, the whole loop stops, and every other plugin and the price feed stop with it. That breaks our
main promise, that a broken plugin cannot hurt the others. Different people write these plugins, so we
cannot assume they will all be careful. Threads avoid this, because the system can interrupt a thread
on its own (see section 3). The cost is small. A plugin cannot pause to wait for a fill, and each one
uses a real OS thread instead of a lighter coroutine. For a few plugins that mostly wait on data, that
is nothing. Async would only pay off with thousands of plugins, which we do not have.

**`submit_order` answers right away, fills come later.** These are two different questions. "Did my
order get accepted?" is quick: check the limits, send it, hear back. So `submit_order` returns
`resting`, `filled`, or `rejected` (with a reason) on the spot. "Did my resting order actually trade?"
might take seconds, or never happen, so it arrives later through `on_order_update`. We could have made
`submit_order` hand back something you wait on for the fill. It reads nicely, but then you are waiting
on something that may never come, and it blurs "a limit rejected me" with "nobody traded with me".
Keeping the two apart keeps each answer simple.

**You are handed `ctx`, you do not inherit it.** A plugin gets one small object, and that object is its
only way out. We could have put `submit_order` and `subscribe` on the `Plugin` base class instead. We
did not, because then the base class holds the real services, and a plugin could reach around the
front door into them. `ctx` is one clear handle: easy to see, impossible to slip past, and easy to
fake in a test (the guide shows a fake `ctx` that just records orders). The only cost is saving
`self.ctx = ctx` in `on_start`.

## 3. Isolation model

**The choice: one process, one thread per plugin, each with its own bounded queue.** That thread and
its queue are the wall between plugins.

**Who are we protecting against?** The plugins here are buggy, not hostile. Different engineers write
them, and they will throw errors, loop forever, or block by mistake. They are not trying to attack the
platform. So we guard against accidents, not attacks. That one assumption is what makes running them in
a single process reasonable.

**What the wall stops:**

- **Errors.** Every hook runs inside a try/except. If it throws, we catch it, log it (it shows on the
  status page), and move on to the next event. The error never reaches the platform or another plugin.
- **Infinite loops.** Python switches between threads every few milliseconds on its own, so a plugin
  stuck in a loop only ties up its own thread, which is one core. The price feed and the other plugins
  keep running. Its own queue fills up and drops old events, so memory stays bounded. While it is stuck
  it just stops placing orders. It only hurts itself.
- **Slow plugins.** The websocket thread drops each tick onto every queue and moves on. It never waits
  for a plugin. A slow plugin falls behind on its own queue and nowhere else.

Through all of this the shared services stay safe, because the order path runs under one lock, and the
store and the market-data fan-out are each locked too. Two plugin threads cannot corrupt shared state.

**What it does not do** (worth saying plainly):

- **It cannot force-kill a plugin.** You cannot safely kill a thread in Python. A plugin stuck in a
  loop keeps burning a core until the whole process exits. On shutdown we ask every plugin to stop,
  wait a short while, and quarantine any that ignore us: we abandon the thread and exit anyway.
  Shutdown still finishes on time, but we never get that core back.
- **It cannot survive a hard crash.** Threads share one block of memory, so a C-level crash, an
  `os._exit`, or running out of memory takes everything down together.
- **Plugins do not run truly in parallel** (Python's GIL). That is fine here, since strategies wait on
  data rather than crunch numbers. Heavy computation would need separate processes anyway.

**What we turned down:**

- **Calling plugins directly on the websocket thread.** The simplest option, but one slow plugin would
  freeze the feed and everyone else. No isolation.
- **A single async event loop.** No way to interrupt a stuck plugin, so one bad loop freezes them all
  (see section 2).
- **A separate process per plugin.** This is real isolation. You can kill a runaway, survive a crash,
  run in parallel, even cap each plugin's memory and CPU. But every tick has to be copied and shipped
  to each process, orders and fills cross the boundary, and the single source of truth and the risk
  checks now have to work across processes. That is a lot more plumbing and a lot more to go wrong
  (dead processes, full pipes, orphans), and it is slower to start. Too much for a few cooperative
  plugins on testnet. It is the first thing we would reach for to run untrusted or CPU-heavy plugins.
- **A sandbox** (locked-down interpreter, container, wasm). The strongest option, and the right one for
  untrusted code from strangers. Overkill for a testnet demo, and it fights the goal of cloning and
  running in minutes.

So we picked the middle. The system can interrupt a stuck plugin, catch its errors, and keep the feed
flowing, which an event loop or direct calls cannot do. And it stays in one process with light
locking, which is far cheaper than separate processes. It fits what the plugins actually are, and the
goal of cloning and running in ten minutes.

## 4. Async / concurrency model (as the plugin author sees it)

From inside a plugin, the world is simple: **your four hooks never run at the same time.** The previous
call always finishes before the next one starts. So you can read and write `self.*` freely. No locks,
no `async`, no surprises.

Here is why that holds. Each plugin has its own thread and its own inbound queue. Events land on the
queue (a market tick, or a later fill update), and your thread takes them one at a time. When you call
`ctx.submit_order`, it runs right there on your thread and returns before the next event is touched.
Even a fill that arrives "later" comes to you as an `on_order_update` on that same thread, in line with
everything else. You never get a callback in the middle of another.

Different plugins do run at the same time, on different threads. But they never share memory, so they
cannot step on each other. The only things they share are the platform's services, and the platform
makes those thread-safe so the author never has to think about it. In particular, the order path takes
one lock, so if two plugins submit at the same moment they are handled one after the other. The order
rate is capped anyway, so that wait is tiny.

The one rule to respect: **do not block forever in a hook.** A slow hook only delays your own plugin,
and only drops your own old ticks, but it does delay you. Keep hooks quick, and do nothing that waits
on the network inside them.

## 5. Rate-limit budgeting across competing plugins

Three limits, all **global** (counted across every plugin, never per plugin): orders per second, open
orders at once, and gross notional open at once.

**One gate.** Every order passes through the **Risk Guard** before it is placed. Because there is
exactly one checkpoint, the promise "no plugin can exceed a global limit" is actually enforceable. If
an order would cross a limit, `submit_order` comes back `rejected` with a plain reason. It is never
dropped quietly.

**A service of its own, separate from placing orders.** Checking and placing are different jobs, so
they are different things. The Order Manager just sends orders to the venue. The Risk Guard just
decides yes or no. Keeping them apart means all the limit logic lives in one place, and the executor
stays dumb.

**Where the numbers come from.** Two of the limits (open orders, gross notional) are about order state,
so the guard reads them from the Order Store, the single source of truth. It does not keep its own copy
that could drift. The third limit (orders per second) is about timing, not order state, so the guard
keeps that itself: a small sliding window of the last second's submissions.

**Check and reserve happen together.** The check and the reservation run under the order path's single
lock: the guard checks the limits, the rate window ticks, and the store records the order as `pending`,
with nothing able to slip in between. So two plugins racing for the last open slot cannot both get
through. The budget is shared, first come first served. A busy plugin can use more of the per-second
budget than a quiet one, and we accept that. There is no notion of priority here, and per-plugin quotas
would be more machinery than a prototype needs. The brief asks for global limits, not fair shares.

**One sensible exception.** A reduce-only order skips the notional cap, because it can only shrink a
position. Otherwise a plugin that is already at the limit could never trade its way back down.

(Gross notional is the notional of *open orders*, size times limit price, kept as a running total so
the check stays O(1). Counting live position notional instead is a simplification, other approach would
be considered if more time was given.)

## 6. Persistence & restart story

**Order states.** Every order moves through a short, one-way lifecycle:

- `pending` — written down before we send it, not yet confirmed by the exchange.
- `resting` — accepted, sitting on the book.
- `filled` — it traded.
- `cancelled` — we cancelled it.
- `rejected` — it never got in (with a reason).

The states only move forward: `pending` then `resting` then one of `filled` / `cancelled` /
`rejected`, and those last three are final. This matters because fills can arrive late, twice, or out
of order. We rank the states and only ever move up, so a stale or duplicate message can never knock a
finished order back to `resting`. Correctness does not depend on message timing or the clock.

**Where orders are held.** The Order Store keeps two layers, kept in step under one lock:

- **On disk: a SQLite database.** One row per order, keyed by its `cloid` (the client order id we
  attach to every order). This is the record that survives a restart. SQLite is embedded: one file, no
  server to run, nothing to install, so it does not slow down clone-and-run. It runs in WAL mode, which
  lets you read the file (for example with `litecli`) while the platform is still writing to it.
- **In memory: the working set.** A dictionary holding only the *open* orders (`pending` and
  `resting`), plus a running gross-notional total. The hot path, the limit checks, reads from memory,
  so it is instant. Once an order is final it is dropped from memory and lives on only in SQLite. So
  memory is bounded by how many orders are open right now, not by all of history.

The rule that ties the two together: **the in-memory set always equals the database's open orders.**
Every change writes through to both at once, under the lock. On startup, memory is rebuilt from the
database.

**How a write works.** Before sending an order we record it as `pending`. So if we crash mid-send, the
database still holds an anchor and we know an order with that `cloid` was attempted. When something
then happens to it, one `apply` step advances its state in both layers. `apply` is idempotent: an
update that would not move the order forward is simply ignored.

**Safe restart.** On startup the store reopens the file and rebuilds its working set from the orders
that were still open. So the platform comes back knowing every order it had open, and the global limits
are enforced against that set from the very first tick. A restart does not lose track of what the
platform placed. Two things are deliberately *not* restored:

- **Plugin state starts fresh.** A strategy's in-memory state (a rolling mean, an inventory counter) is
  not persisted, so `on_start` runs again and rebuilds it from new data. We persist platform facts
  (orders), not strategy internals.
- **Positions live on the exchange,** not in our database, so they survive a restart on their own, and
  even survive deleting the database. `--fresh` deletes the database for a clean start.

P&L is restored from the stored fills, but it is `position × current mark`, and the mark comes from
live data that is not persisted. So just after a restart P&L reads null until the first price tick
provides a mark (sub-second), and the position is then re-marked at the current price, not the price
when you stopped.

**The honest gap.** We rebuild our own record of open orders, but in live mode we do not yet
*reconcile* against the exchange. If an order filled or was cancelled while the platform was down, the
new run will not notice, and a resting order from the previous run will not be auto-cancelled, because
the live executor's in-memory link from `cloid` to coin is not rebuilt. This is the first thing we
would add with more time.

**Why a database, not just a file.** The brief rules out in-memory-only, and we needed durable, atomic
updates plus the ability to read state live. SQLite gives all three with zero setup. A plain JSON or
append log would mean writing our own atomic-update and consistency logic by hand. A database server
like Postgres would add operational weight and fight the ten-minute clone-and-run goal. SQLite is the
smallest thing that is actually safe.

**Why one source of truth.** Every component asks the store for order state instead of keeping its own
copy, so nothing can disagree. The Risk Guard reads its numbers from there. And because we support
restart, that one source has to be persistent, which is why it is a database wrapper and not just a
dictionary in memory.

## 7. The single most important tradeoff, and a week to revisit it

**The tradeoff we made: threads, not processes.** Every plugin runs as a thread in one process,
sharing the services and the address space. We chose this for simplicity: no IPC, one copy of each
service, fast to start, easy to read. The cost is real, and we named it in section 3. We cannot
force-kill a runaway plugin, only quarantine it, so a plugin stuck in a tight loop keeps burning a core
until the process exits. And because everything shares one address space, a native crash (a
C-extension segfault, an out-of-memory) takes the whole platform down with it. For a handful of
cooperative plugins on testnet this almost never bites, which is why it was the right call for two
days. It would not be the right call for a real, multi-author deployment.

**The week's fix for that: a process per plugin.** Each plugin runs as its own OS process. The parent
still owns the store, the risk guard, and the executor, so the single-source-of-truth design does not
change. What changes is the wiring: market data goes out to each child over a pipe, and order intents
come back from the child to the parent, where the same gate (check, reserve, place) runs exactly as it
does today. The win is hard isolation. The parent can `SIGKILL` a child that stops answering a
heartbeat, set a CPU and memory cap per child (rlimits or cgroups), and survive a child that segfaults.
The cost is that every tick must now be serialized and copied across the pipe, and there are more
moving parts to get wrong (dead children, full pipes, orphans). At our scale that cost is affordable,
and we already have the seam for it: plugins only ever touch the platform through `ctx`, so `ctx`
becomes a thin client that talks to the parent instead of calling in-process.

**But if I am being critical, this is not what I would fix first.** The thread tradeoff only hurts us
under a *bad* plugin. A sharper problem hurts us under *normal* operation: on a live restart, we reload
our own database but never check it against the exchange. We come back believing whatever we last wrote
down. If an order filled or was cancelled while we were down, we do not know it. If an order is still
resting, we cannot even cancel it, because the executor's in-memory link from `cloid` to coin is empty
after a restart. So our open-order count, our gross-notional limit, and our P&L can all be quietly
wrong, and a strategy might re-place an order it already has live. For a platform whose whole job is to
not lose track of orders, that is the most important hole.

**The fix: reconcile on startup, then keep reconciling.** It is cheaper than it sounds, because the
`cloid` already ties our records to the venue's, and `apply` is already idempotent and advance-only. On
startup, after rebuilding the working set from disk, ask the exchange two questions: which orders are
open for this account, and which fills happened recently. Then merge each of our open orders against
the answer:

- We think it is resting and the exchange still shows it open: rebuild the executor's `cloid`-to-coin
  link, so we can manage and cancel it again.
- We think it is resting and the exchange does not show it: it filled or was cancelled while we were
  down. Pull the fill from the recent-fills answer and `apply` it, or mark it cancelled.
- The exchange shows an order we have no record of: flag it for a human. It should not happen, but a
  platform that places real orders should never hide an order it cannot explain.

Run the same merge on a timer while live, not only at startup, so a missed websocket message self-heals
instead of drifting. None of this needs new storage or a new correlation key. It reuses what we already
built.

A week buys both: subprocess isolation for resilience, and reconciliation for correctness. I would
build reconciliation first, because a wrong view of live orders costs money, while a runaway plugin
only costs a core.

## 8. Note on AI-agent use

I used Claude Code as an implementer and a sounding board, but I drove the design.

I proposed the overall shape: break the platform into small services, each holding one concern (market
data, order execution, persistence, risk limits, observability), so the concerns stay separate. We
then talked through how they should call each other, and in what order, to keep their interdependence
low. That is how the order store became the single source of truth, with the risk guard reading its
numbers instead of keeping its own copy.

We worked out the plugin model together. I wanted a plugin to be simple for someone else to write. We
landed on giving each plugin its own thread and an inbound message queue, which turns a plugin into a
plain, thread-safe object: its callbacks never overlap, so the author needs no locks.

Throughout, I pushed for simplicity and robustness. I simplified the code and the docs, proposed a
cleaner project layout (a `services/` and `contract/` split, with plugins kept outside the package),
and insisted we handle the failures that matter: websocket drops, a crashing or looping plugin, and a
clean shutdown even on repeated Ctrl+C.

Claude handled the implementation, the tests, and checking the SDK against its source and docs so we
were not guessing, plus the live testnet checks. I reviewed all of it and corrected it where it
drifted: a stale import after a refactor, an over-verbose guide, and a config flag that should have
been a positional argument.
