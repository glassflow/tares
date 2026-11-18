# Console design critique, 2026-11-12

Baseline scorecard for the console design system project (TR-1255), taken before any of its fixes land.
Method: the `/critique` skill against the design context in `.impeccable.md`, on the integration branch
`vladimir/console-design-system` at `0d21938d`. Two assessments ran without seeing each other. One was
a design review of the screens and the source. The other was the deterministic `impeccable` detector
over `ui/src`. Screens come from the repo's own e2e suites (12 suites, both editions), run against
seeded sample data in scratch daemons on a laptop. Light and dark come from the same runs, with dark
forced through `prefers-color-scheme`. Widths are 1440 and 390. Three legacy pages (`/sources`,
`/sources/claude-code`, `/resources`) were shot separately. The 39 screenshots are in
[`2026-11-12-console-design-critique/`](2026-11-12-console-design-critique/), numbered as in the screen
table below.

Re-run the same way after M3 and M4 to measure the change.

## Design health score

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of system status | 3 | SRE Home's incident chart draws errors flat at 0 while the headline says 1 in 12 requests return 502 (09). |
| 2 | Match between system and real world | 3 | Time in four dialects on one page: `Nov 12, 07:50 PM`, `9 Nov`, `Sat 12:40`, `10 days`, all inside cells. |
| 3 | User control and freedom | 2 | Runbooks has Remove on every line with no undo (13). Mark resolved sits beside I'm on it at the same weight (09, 11). Close project is dressed like Open (03). |
| 4 | Consistency and standards | 1 | One state, several dresses: `CHIP_TONES` in `FactoryHome.tsx:49` and `CHIP` in `FactoryProjects.tsx:38` draw `building` green and neutral. Four badge families on one Ticket page (05). |
| 5 | Error prevention | 3 | Allow ↗ on Crew grants `gh pr merge` but looks like a secondary link (07). |
| 6 | Recognition rather than recall | 3 | Decision codes such as `D79 A`, `D40 A` quoted as if they were a grant, with no words beside them (08, and the decision lists). |
| 7 | Flexibility and efficiency of use | 2 | No keyboard shortcuts, no bulk accept on Reviews or Tickets. |
| 8 | Aesthetic and minimalist design | 2 | A box around every section, nested three deep on Ticket; amber on filters and eyebrows. |
| 9 | Error recovery | 3 | The error state blanks the page under a rose-tinted fill (16); the sentence itself is good. |
| 10 | Help and documentation | 3 | Project page needs a help line for its arrow buttons; copy elsewhere explains the mechanism well. |
| **Total** | | **25/40** | **Acceptable** |

Cognitive load: 4 of 8 checks fail (high). The single focus fails on SRE Home: a checklist sits above
the incident. Hierarchy fails on Factory Home, with six outline buttons of equal weight. Choices fail
in three places: Projects has 8 tabs, Tickets 8 toolbar controls and Timeline 11. Working memory
fails on the D-codes.

## Anti-patterns verdict

**Does it look AI-made? Mostly no.** The paper, the brown ink, mono labels and computed sentences are a
real direction, and the Incident page reads as designed. The tells sit in the details:

- Side-stripe callouts in components: `editions/factory/FactoryProject.tsx:678,686`,
  `FactoryTicket.tsx:80`, `FactoryCalls.tsx:30`.
- A box per section instead of content on the paper, nested three deep on the Ticket page (05).
- A "Good evening" greeting as the Factory Home H1 (01).
- A swimlane card grid on Tickets (04).
- A `✦ Auto-discover` sparkle on the legacy Sources page (`pages/Sources.tsx:92`, 14).
- No Doto anywhere. The headline numerals ("Today 1 / 1" on SRE Home) are Inter (TR-1270).

**Deterministic scan.** `npx impeccable --json src` from `ui/` exited 2 with 5 findings, all
`side-tab`, all in `ui/src/styles.css`:

| Line | Selector | Verdict |
|---|---|---|
| 1394 | `.md blockquote` (3px `--line`) | False positive: a quote mark in rendered markdown. |
| 1590 | `.builder-brief blockquote` | False positive, same reason. |
| 1930 | `.main ul.su-problems` (3px `--err` on `--err-soft`) | Real: a side-stripe error callout. |
| 2027 | `.fo-gist` | Real: a stripe used as decoration. |
| 2065 | `.fo-said` (3px `--warn`) | Real, and amber outside its three meanings. |

The two assessments complement each other. The detector reads stylesheets and found only the CSS
stripes. The review found the four stripes written as Tailwind classes in TSX, which the detector does
not see. Together that is seven real side stripes and two legitimate quote marks.

## Overall impression

A console with a real point of view and honest copy, let down by inconsistency rather than by taste.
Every page explains itself in plain sentences, and the best screen (Incident, 11) is already what the
direction asks for. The rest drifts: each page dresses state its own way, boxes stand in for rules, and
amber appears wherever something wanted emphasis. **The biggest opportunity is one state vocabulary**:
one component, one set of words, one colour each, used on every page. It would fix the lowest score
(consistency, 1/4) and most of the noise at once.

## What's working

- **The Incident page (11).** What failed, the likely cause and the next step, a figure, a timeline,
  one primary action. It holds at 390px with nothing cut off. This is the template for the rest.
- **Copy that states the mechanism.** "Checked every 10 minutes, last 3 minutes ago", "It last sent on
  9 Nov, 20:30", "Every check since then came back quiet". Plain, specific, verifiable.
- **Running head plus a computed sentence.** "1 incident open on service-a" followed by "2 of the other
  5 services look normal" is exactly the paper pattern, and both editions use it.

## Priority issues

**[P1] SRE Home puts onboarding above the fire.**
*Why:* the on-call reader arrives from a notification for the incident. The "Get started" checklist
renders first (`editions/sre/Home.tsx:268`), so at 390px the incident starts about 700px down (09).
*Fix:* when an incident is open, render the incident first and fold the checklist below it, or collapse
it to one line. *Command:* /layout. *Ticket:* none yet.

**[P1] The same state wears different clothes.**
*Why:* a reader learns "green chip = building" on Home, then meets a neutral chip for the same state on
Projects. Blocked segments are hatched amber on Home and hatched red on Projects. Four badge families
appear on one Ticket page. Nothing can be learned once.
*Fix:* one `StatusLabel` and one state map for every edition, as `.impeccable.md` already decides.
*Command:* /distill. *Ticket:* TR-1265.

**[P1] Amber outside its three meanings.**
*Why:* amber should mean primary action, selected edge or live work. Today it also marks metric tags on
Timeline (`editions/sre/Timeline.tsx:138`, "drawn amber whatever the cell's tone"), section eyebrows, the
memory bar on Runbooks, "Holds up" on Reviews and the `.fo-said` stripe. When everything is amber, the
one I'm on it button stops standing out.
*Fix:* take amber off tags, eyebrows and bars; draw them in ink or the neutral underline.
*Command:* /quieter. *Ticket:* TR-1265 covers waiting; the rest has no ticket yet.

**[P2] Panels and stripes instead of paper.**
*Why:* a box around every section flattens hierarchy (everything is equally framed) and is the anti-
reference the context names. Seven side stripes remain (see the scan).
*Fix:* rules and whitespace between regions; stripes become a state word or a plain sentence.
*Command:* /distill. *Ticket:* TR-1257 covers radius, shadows and table heads only.

**[P2] Factory Home has no primary for the decision.**
*Why:* the owner comes to answer what waits on them, but six outline buttons share one weight, and the
proposed rules the crew needs approved are on Crew, not Home (01, 07).
*Fix:* one primary action for the top decision; surface pending rule proposals on Home.
*Command:* /layout. *Ticket:* none yet.

**[P2] Time grammar.**
*Why:* four time formats on one page make comparison hard and break the "instant = UTC stamp" rule.
*Fix:* the single time module. *Command:* /clarify. *Ticket:* TR-1261.

**[P3] The legacy pages look like another product.**
*Why:* `/sources` and `/resources` (14, 15) predate the kit: different type, different buttons.
*Fix:* move them onto the edition kit. *Command:* /polish. *Tickets:* TR-1250, TR-1259, TR-1262, TR-1267.

## Screen by screen

| # | Screen | Verdict |
|---|---|---|
| 01 | Factory Home | Right content, flat hierarchy. In dark, the READY TO CLOSE chip is the loudest thing on the page. |
| 02 | Projects | 8 tabs is too many. Blocked bars are red here and amber on Home. |
| 03 | Project | Dense but honest. A heading says "4 things" over a list of 3. |
| 04 | Tickets | A busy board; swimlanes of cards for what is a list. |
| 05 | Ticket | Boxes in boxes, three levels deep; four badge styles. |
| 06 | Reviews | Repetitive rows; a stray `7 –` glyph; amber "Holds up". |
| 07 | Crew | A good readout. The rules waiting for approval are buried. |
| 08 | Settings | Plain and fine. The avatar initials render as `A(`. |
| 09 | SRE Home | The fire sits below onboarding. The incident chart contradicts its headline; service sparklines are decoration. |
| 10 | Incidents | Calm and clear. |
| 11 | Incident | The best screen. The figure lacks a unit and an axis label. |
| 12 | Timeline | Colour overloaded; 11 controls; filter toggles in amber. |
| 13 | Runbooks | Clear. Remove on every line, no undo. |
| 14, 15 | Legacy Sources | Another product: different type, buttons and the sparkle. |
| 16 | States | Error, empty and loading are correct and generic. The skeleton mimics the boxes. |

In dark (TR-1271), incident sections read as stacked boxes on a saturated brown, and chrome and
content share one tone. That is the V2 ladder's job.

## Persona red flags

- **Alex, power user.** No keyboard shortcuts on any list. No bulk accept on Reviews or Tickets.
- **Sam, keyboard or screen reader.** State shown by colour alone: crew dots, service squares on SRE
  Home, Connected chips, Timeline toggles. Allow ↗ needs an accessible name that says what it grants.
- **Casey, on a phone.** The incident is about 700px down on SRE Home at 390px; range buttons wrap on
  Timeline (12).
- **Factory owner, at a desk.** No primary action for the decision waiting on them; rule proposals live
  on Crew.
- **AI SRE on call.** The chart says nothing is wrong while the headline says 1 in 12 requests fail.
  Mark resolved sits next to I'm on it at the same weight.

## Minor observations

- Two link styles: underlined text links and plain ones that look like labels.
- "Show 1 more" is a button on one page and a link on another.
- The selected nav item is a filled block in dark and an accent bar in light.
- Loading skeletons copy the boxes, so they will need to change with them.
- Hatched "blocked" segments are amber on Home and red on Projects.

## Questions to consider

- Why does Factory Home show Projects at all, when Projects is one click away and the decision is not?
- Is the swimlane board needed, or is Tickets a sorted list with a state column?
- When exactly one incident is open, should SRE Home open on it?
- What would the Ticket page look like with zero boxes?

## Suggested order of work

1. One state vocabulary (TR-1265), because every other fix inherits it.
2. SRE Home order and the incident chart (new ticket).
3. Amber budget: tags, eyebrows, bars, stripes (TR-1265 plus a new ticket).
4. Paper over panels and the remaining side stripes (TR-1257, extended or a new ticket).
5. Factory Home primary action (new ticket).
6. Time grammar (TR-1261).
7. Legacy pages onto the kit (TR-1250, TR-1259, TR-1262, TR-1267).
8. Re-run this critique and record the new score here.
