# qmeas — Assistant Knowledge Base

This is the reference the qmeas AI Assistant uses to answer questions. It
describes what a user can see and do in the qmeas interface — nothing about how
files are stored internally, because the user never touches those.

**Provenance.** qmeas is an independent open-source project — developed with
Anthropic's Claude, MIT-licensed, Copyright (c) 2026 Project h_e2. It is NOT a
product of any instrument vendor.

## How to answer

- Answer only about things the user can actually see and do in qmeas: panels,
  dialog fields with their on-screen labels, task-grid columns, buttons.
- **Never invent or display internal formats.** There are no "field numbers",
  no mode/nargs/numfmt/cmdstr codes, no pipe-delimited lines. If you find
  yourself writing any of those, stop — they are not real to the user.
- When the user asks about an **experiment**, answer about task rows and
  structure only. Do NOT drift into how to create or configure a device or
  command unless that is what they asked.
- Describe command settings by their **dialog labels**: Method, Linked read,
  Equals, Timeout, Tolerance, Element, Integration Time, Start, Final, Steps.
- The value placeholder in a write command string is always the literal
  token `[%]` (left-bracket, percent, right-bracket) — never `%` alone.
- Be concise and concrete. If something isn't covered here, say you're not sure
  rather than inventing qmeas behavior.
- Device names in examples are generic (magnet, voltagesource,
  frequencygenerator, lockin) — real setups vary.
- If example exchanges are shown before the conversation, treat them as the
  authoritative pattern: match their structure, ordering, and level of detail
  when a question resembles one of them.
- This lab's specific setup (instrument addresses, bridge start commands,
  ranges) is in the user-notes section below, if present. Prefer it for
  anything installation-specific. If the user asks how to start a particular
  bridge or what an instrument's address is and it's in the notes, answer from
  there; if it isn't recorded, say so and suggest they add it via the
  'User notes' button in the Assistant window.

---

## What qmeas is

qmeas runs measurement sequences against lab instruments (magnets, source-measure
units, lock-in amplifiers, cryostats, and other controllers). You define
instruments and their commands, then build an ordered list of **task rows** that
sweep values and record readings.

Panels:

| Panel | What it's for |
|---|---|
| Devices & Commands | The tree of instruments and their commands. Double-click a command to add it as a task row. |
| Device Editor | Add or edit an instrument's address and connection settings. |
| Tasks | The ordered grid of rows that make up a run. |
| Log | Timestamped record of each executed row. |
| Graph | A separate window that live-plots the current sweep. |

Two built-in devices are always present: **control** (holds pause, userprompt,
stop, counter, and any while-loops you create) and **script** (runs Python files
you place in the custom folder).

---

## Adding a device

In **Devices & Commands**:

1. Pick a device list in the dropdown and press **Load**, or press **New** to
   create a fresh one (you're asked for a name; existing names are rejected). A
   new list already contains control and script.
2. Open the **Device Editor** and press **New Device** for a blank entry.
3. Give it an address:
   - **Scan** finds instruments on VISA (GPIB/USB). LAN instruments do not show
     up in a scan — that's normal.
   - **Add TCPIP…** for a network instrument: its IP and port.
   - **Add HTTP…** for instruments with an HTTP/JSON control interface.
   - **Manual address…** to type an address directly.
4. Enter a device name (lowercase letters and digits), set the termination
   characters and timeout if the instrument needs specific ones, optionally
   **Test Connection**, then **Save Device**. The device is added to the list
   currently loaded in Devices & Commands.

### Duplicating a device

Right-click a device → **Duplicate Device**. The copy gets a unique name
(`keithley1` → `keithley1copy`, then `keithley1copy2`, …) and includes all the
original's commands. Two things are deliberate: the copy has **no address** and
starts **deactivated** — give it its own address in the Device Editor and
activate it before use. Using it unconfigured produces a connection error (by
design, so two devices can never silently share one instrument's address).

## Adding a command to a device

Right-click the device in the tree → **Add Command…**:

- Give the command a name (lowercase letters and digits) and choose **write** or
  **query**.
- Type the command string the instrument expects. For a write that takes a
  value, put the exact token `[%]` where the value goes — written literally as
  left-bracket, percent, right-bracket. For example a voltage-set command is
  `VOLT [%]`, a field-set command might be `FIELD [%]`. (Not `%`, not `<value>`
  — always `[%]`.)
- For a **query**, use **Query Once** to fetch a live reply and click-select the
  part you want to keep (the whole reply, one value from a comma-separated list,
  a substring, and so on).
- For a **write**, choose a **Method** (see below). Press OK.

**A verify or ramp write needs a matching QUERY command on the same device
first.** verify watches a read-back to know when the target is reached, and
ramp reads the present value to step from — so before you can pick it in the
**Linked read** dropdown, that query must already exist. Practical order: make
the read-back query first (e.g. a `readfield` or `readstatus` query), then make
the write and set its Method to verify/ramp with that query as the Linked read.
If the dropdown is empty, you haven't made the read-back command yet.

## Using commands in a measurement

- Double-click a **write** command to add it as a task row. Fill **Constant/Start**
  for a fixed value, or **Start + Final + Steps + Integration Time** to sweep.
- **Readings are not rows.** To record a reading, set that query's **LED** to
  green; it is then saved automatically at every measurement point. (A lock-in
  reading is a green query, never a task row.)
- **Simulate** previews the whole list without touching hardware. **Start** runs
  it; **Stop** aborts.

---

## Write methods (the Method field on a write command)

A write command can set a value in one of three ways:

- **none** — send the value and move on immediately. Fine for instruments that
  respond instantly (a voltage source, a frequency setpoint on a fast source).
- **ramp** — qmeas walks the value to the target in software steps at a rate you
  give. Needs a **Linked read** (a query on the same device that reports the
  present value). Ramp is a qmeas-side stepping of the value; it is for devices
  that reach each value INSTANTLY but should be moved GENTLY (e.g. a voltage
  source on a sensitive sample). **Ramp is NOT for magnets or other slow
  actuators** — a device that takes time to reach its setpoint needs **verify**,
  which waits for arrival, not ramp, which just sends smaller steps and does not
  wait. (Ramp is also unrelated to a magnet controller's own internal ramp.)
- **verify** — after sending the value, qmeas waits until a **Linked read**
  reports that the target is reached before the run continues. This is how a
  step *waits* for hardware. There are TWO ways to set the "reached" test:
  - **Option A — status flag (the good option, use this for magnets):** point
    **Linked read** at a query that returns the device's STATUS, and set
    **Equals** to the settled status word that query returns. Tolerance does
    not apply here (it is a text match, not a number). This is robust and it
    works for a swept setpoint too, because the status word is the same at
    every step.
  - **Option B — field value within a tolerance (use when there is no status
    flag):** point **Linked read** at the value read-back and set **Equals** to
    the literal token `[%]` (the row's own setpoint) with a non-zero
    **Tolerance** (the reading never hits the exact digits, so a zero tolerance
    would wait forever). Because **Equals = [%]** follows the row's setpoint, it
    works for a swept field as well as a fixed one. (Only if you typed a fixed
    number into Equals instead of `[%]` would it stop tracking a sweep.)
  Tolerance is meaningful ONLY in Option B (it is a numeric band); it has no
  meaning for a status word. The exact status word and the query string are
  instrument-specific — find them with **Query Once** on the read-back and copy
  what the device actually returns; do not assume a particular spelling.

### Magnets and other slow actuators — important

A magnet does **not** reach its field instantly: the controller ramps for
seconds to minutes after the command is sent. The same is true of temperature,
or a laser that needs to lock. If such a device is set with method **none**, the
run moves on while it is still changing, and data is recorded at the wrong
value.

A magnet needs TWO commands: a WRITE that sets the field and a QUERY that reads
back. The read-back can be EITHER the field value OR a status word — the user
picks one, and that choice determines how Equals/Tolerance are filled. Present
it exactly like this:

1. Create the read-back QUERY. Name it (e.g. `readfield` or `status`). Its
   command string reads either the field VALUE (e.g. `FIELD?`) or the magnet's
   STATUS (e.g. `STATE?`). Command strings are instrument-specific — confirm
   with Query Once.
2. Create the WRITE that sets the field. Name it (e.g. `setfield`), command
   string `FIELD [%]`.
3. On the write, set Method = verify, and Linked read = the query from step 1.
   Then fill Equals/Tolerance according to WHICH read-back you made:
   - **If the read-back is a FIELD VALUE:** set Equals = `[%]` (the literal
     token — it means "this row's own setpoint") and set a reasonable non-zero
     Tolerance below it. (Do not leave Tolerance at 0 — the reading never hits
     the target exactly.)
   - **If the read-back is a STATUS word:** set Equals = the settled status word
     (e.g. `HOLD`). No Tolerance — it is a text match, not a number.
   Also set a Timeout.

Never mix the two: a status word never takes a Tolerance, and a field-value
read always needs Equals = `[%]` plus a Tolerance. Both forms track a swept
field (the status word is the same at every step; `[%]` follows each setpoint).
In a nested measurement the inner sweep proceeds only once verify passes.

---

## Query LEDs (green / red / grey)

Every query command has an LED you click to cycle:

- **green** — read and **saved** to the data file at every point. Use this for
  anything you want to record.
- **red** — read but **not saved**. Still available to a verify or while-loop
  that watches it. Use for a status read-back (like a magnet's settled-status
  word) you don't want cluttering the data.
- **grey** — off; not read at all.

The choice is remembered between sessions. If a status word or an error message
is showing up as a column in your data, that query is green — click it to red.

For a query that returns several values at once (a lock-in returning X and Y,
say), when you point a verify or while-loop at it an **Element** field appears so
you choose which value to watch (0 = the first).

---

## The task grid

Columns: **On** (unchecked rows are skipped), **Command**, **Constant/Start**,
**Final**, **Steps**, **Integration Time**, **Comments**.

- A row with only **Constant/Start** filled sets a single value.
- Fill **Final**, **Steps**, and **Integration Time** as well to sweep.
- **Integration Time** accepts 2s, 0.5s, 2m, or a bare number of seconds.
- Fields turn **red** when a row is inconsistent — e.g. you started a sweep but
  left Steps or Integration Time blank, or typed something non-numeric. It's a
  guard; fix the red fields before running.

The **control** commands in a row: pause waits (Constant/Start = seconds, or
5m, 1h), userprompt pops up a message and waits for you, stop aborts the run,
counter steps a plain index/time axis (useful as a time base or the driver of a
linked group).

---

## Nesting (a sweep inside a sweep)

Nesting runs an inner sweep completely at each step of an outer sweep — for
example a frequency sweep at every gate voltage.

**The one rule people get wrong:** you nest the **inner** row. Select the row
that should run *inside*, press **Nest**, and it attaches to the row directly
**above** it, indenting one level. You never press Nest on the outer row.

How to build it (bottom-up):

1. Add the **outer** sweep as the first row (Start, Final, Steps, Integration
   Time).
2. Add the **inner** sweep as the next row below it.
3. Select the **inner** row and press **Nest**. It indents under the outer row.
4. Turn the readings you want green. **Simulate** to check the structure, then
   **Start**.

You can go up to 4 levels deep (add each deeper row below and Nest it).
**Unnest** removes nesting from the bottom row.

The saved data file has one line per innermost point, with a count-and-value
pair for each outer level, then the elapsed time, then your green readings.

### Worked example — frequency inner, voltage outer

Assume voltagesource_setvolt and frequencygenerator_setfreq commands exist, and
the lock-in reading is green.

1. Double-click voltagesource_setvolt → first row (outer). Start -1, Final 1,
   Steps 21, Integration Time 0.5s.
2. Double-click frequencygenerator_setfreq → next row (inner). Start 1, Final
   10, Steps 101, Integration Time 1s.
3. Select the **frequency** row and press **Nest** — it indents under the
   voltage row.
4. Simulate, then Start. For each of the 21 voltages, frequency sweeps all 101
   points and the reading is recorded at every point.

### If a magnet is the outer sweep

Same structure, but the magnet field-set row must use the **verify** method (see
the magnet note above), so each field value is actually reached before the inner
sweep runs. Use verify on the magnet row (status-flag is simplest: Linked read = the status
query, Equals = the settled word; or field-value: Linked read = the field query,
Equals = `[%]`, with a tolerance) — both track a swept outer field. The inner
sweep is still the
row you press Nest on — never the magnet row.

---

## Linking (compute one value from another)

Linking makes a **follower** row compute its value from a **driver** row's
current value, each step. In the follower's **Constant/Start** you write an
expression using the exact token `[%]` for the driver's value — e.g. `[%]*2`
or `[%]+0.5` — with the usual arithmetic and functions like sin, cos, sqrt,
abs. A driver plus up to three followers form one group (all at the top
level). Use it when one quantity should track another (a compensating gate, a
symmetric bias).

Nesting and linking can't both apply to the same row.

---

## While-loops (wait until a condition)

A while-loop is a control command that repeats — reading a chosen query each
time — until a condition is met, then lets the run continue. Right-click
control → **Add Virtual While…**: pick the query to watch, an operator
(<, >, =, !=), a value, and a mandatory **Timeout** (0 = wait forever). Its
**Integration Time** is how often it polls. Use it to wait for a temperature to
fall below a threshold, a lock to acquire, and so on — the wait is visible in
the run, abortable, and its readings are logged. (= and != compare text
exactly; < and > need a numeric reading.)

---

## Running, simulating, saving

- **Start** runs top to bottom; **Stop** aborts (magnets are held safely on
  abort if you've set that up). **Simulate** walks the list and describes what
  each row would do, without touching hardware — use it to check a new setup.
- **Save/Load** store and restore the task list; **Clear** empties it.
- Each sweep writes its own tab-separated data file (timestamped) as it runs, and
  the Graph window plots it live.

---

## Instruments with no standard interface (bridges)

Some instruments can't be reached over a normal network/VISA connection — they
only offer vendor software or a vendor library. qmeas reaches these through a
small **bridge** program (shipped in the qbridge folder) that runs alongside the
vendor software and presents the instrument to qmeas as an ordinary network
device. To qmeas it then looks like any other instrument added with **Add
TCPIP…**.

Typical pattern, e.g. a cryostat magnet/thermometer or a tunable laser source:

1. On the PC that can talk to the instrument, make sure the vendor software (and
   its server, if it has one) is running.
2. Start the bridge program for that instrument from a command line (each bridge
   has its own start command; it prints "adapter ready" when connected). It
   listens on a port — 5100 by default; give a second bridge a different port.
3. In qmeas, **Add TCPIP…** pointing at the bridge's host and port (the same PC
   and 5100 if the bridge runs locally), and define commands normally.

Important: qmeas connects to the **bridge**, not to the instrument's own vendor
port. Each measurement reading briefly opens and closes a connection to the
bridge — that's normal, not an error. If readings fail, check that the vendor
software, its server, and the bridge are all running (start them in that order);
a bridge recovers on its own from a brief interruption.

If a reading comes back as READ FAILED, the instrument or its bridge didn't
answer — check the connection and that the bridge is running.
