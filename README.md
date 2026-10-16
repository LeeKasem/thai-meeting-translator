# Meeting Translator: English ⇄ Thai (Azure Speech)

A Windows desktop app that listens to a meeting or conversation, shows live
captions, and reads the translation aloud. It works in both directions:
English → Thai is read with **th-TH-NiwatNeural**, Thai → English with
**en-US-JennyNeural**. The voice is picked from the target language of each
sentence and can be changed in `.env`.

```
Audio (mic or system loopback)
  → 16 kHz mono PCM → Azure TranslationRecognizer
      English → Thai | Thai → English | Auto-detect (experimental)
  → caption + history + auto-saved transcript
  → TTS queue, voice per target language → your chosen output device
```

> **Tell participants.** Meeting audio is sent to Microsoft Azure for
> transcription and translation. Let everyone know the meeting is being
> transcribed and translated before you start.
>
> **It can be wrong.** Machine translation and synthetic speech make mistakes,
> especially with names, numbers, jargon and overlapping speakers. Use the
> output as an aid, not as an official record.

---

## Free tier (F0) limits

Checked on 2026-10-16 on Microsoft's official pages:

| Item | Free (F0) allowance | Source |
|---|---|---|
| Speech translation | **5 audio hours / month** | [Azure Speech pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/) |
| Neural text to speech | **0.5 million characters / month** | same page |
| Concurrent speech-to-text + translation sessions | **1** (not adjustable) | [Quotas and limits](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits) |
| Real-time TTS requests | **20 per 60 seconds** | same page |

What this means in practice:

- 5 hours of translation is about five one-hour meetings a month. The app
  streams audio continuously, silence included, so every minute the app is
  running counts.
- Only one session can run at a time. Close any other app that uses the same
  key before you press Start.
- The app limits itself to 20 TTS requests a minute.
- Prices and limits change, so check the pages above again.

The usage line at the bottom of the window shows this session's audio
minutes and TTS characters, plus a month-to-date total kept in `usage.json`.
It turns red at 80% of the free allowance. That total only counts this app on
this PC. The authoritative numbers are under **Azure portal → your Speech
resource → Monitoring → Metrics**.

---

## 1. Create the Azure Speech resource (F0)

1. Sign in at <https://portal.azure.com>. A free Azure account works.
2. **Create a resource**. Search for **Speech** and choose the **Speech**
   service from Microsoft. Click **Create**.
3. Fill in the form:
   - **Subscription / Resource group:** any. Create a new group such as `meeting-translator`.
   - **Region:** one close to you, e.g. **Southeast Asia** (`southeastasia`).
   - **Name:** any unique name.
   - **Pricing tier:** **Free F0**.
4. Click **Review + create**, then **Create**, then **Go to resource**.
5. Open **Keys and Endpoint** (under Resource Management). Copy **KEY 1** and
   the **Location/Region** value (e.g. `southeastasia`).

## 2. Install

Requires Windows 10/11 and Python 3.10+.

```powershell
cd thai-meeting-translator
py -3.10 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env`, set:

```
AZURE_SPEECH_KEY=<KEY 1 from the portal>
AZURE_SPEECH_REGION=southeastasia
```

`.env` is listed in `.gitignore`. Never commit it or share it. You can set
the two values as Windows environment variables instead of using `.env`.

Optional: the voice used when translating *into* each language.

```
TTS_VOICE_EN=en-US-JennyNeural
TTS_VOICE_TH=th-TH-NiwatNeural
```

A voice must belong to its language (`en-…` for English, `th-…` for Thai), or
be a `…Multilingual…` voice. Otherwise Start reports a configuration error.

Easier: choose voices in the app (**Audio and voice → Select a voice**). The
picker lists the English and Thai neural voices for your region, with a ▶
preview. The choice is saved in `settings.json` next to the app and takes
precedence over `TTS_VOICE_*`.

## 3. Run

```powershell
.\.venv\Scripts\activate
python main.py
```

1. **Meeting / Onsite** (the pill switch at the top): *Meeting* for remote
   meetings or listening only; *Onsite* for two people sharing this device
   (section 4b).
2. **Languages:** the left pill is the spoken language (*English*, *Thai* or
   *Auto-detect (beta)*), the right pill the language it is translated into;
   ⇄ swaps them. You can change them while running. The recognizer
   reconnects, so speech during the switch (about a second) is missed.
3. **Audio and voice** (click to expand; it folds away while translating to
   leave room for the conversation):
   - **Listen to:** *System audio* (Teams/Zoom/Meet on this PC) or
     *Microphone* (people speaking in the room).
   - **Input device:** for system audio, pick the `[Loopback]` entry of the
     device the meeting plays on. **Voice output:** where the voice should
     play, ideally headphones. ↻ refreshes the device lists.
   - **Mute voice**, **Pause listening while the voice speaks**, and
     **Select a voice**.
4. Press the round red **mic button**. The hint above it shows the state:
   *Connecting…* (spinner, button disabled so it can't start twice), then
   *Listening · tap to stop* with a ring that follows the input level,
   *Translating…*, and *Speaking the translation* (blue button with a
   speaker). When the mic is paused during the voice, a crossed-out mic badge
   says so. Press the button again to stop.

The conversation shows one card per sentence: what was said (small, grey),
the translation (large), a language badge such as **EN → TH**, the time, and
a ▶ replay button. The sentence being heard right now appears as a **Live**
card. The card being read aloud is highlighted. In *Auto-detect* and
*Onsite*, the two languages sit on opposite sides (English left in blue,
Thai right in green). The list follows the newest card; scroll up and it
stays put, with a **Jump to latest** button. Right-click a card to copy it.

Next to the mic: **🔊 Voice on / 🔇 Voice off** (Ctrl+M) mutes the spoken
translation at any time without touching the mic or the subtitles (see
*Long sessions* below), and **⤢ Subtitles** (F11) opens the projector view.

Top bar: **Aa** large text (for showing the screen to the other person),
**⤢** subtitle window, **⤓** export, **⚙** shortcuts and usage. Light or dark follows your Windows
app mode, and animations are off when Windows *Animation effects* is off.

If Windows blocks desktop apps from the microphone (Settings → Privacy &
security → Microphone), the app says so and links to the setting, instead
of starting and hearing only silence.

**Auto-detect** asks Azure to decide per sentence whether English or Thai was
spoken (continuous language identification). Accuracy and delay have not
been measured with real speech yet. Use a fixed direction for anything
important until they have.

### Running as an .exe (no Python needed)

Build it once on a PC with the project set up:

```powershell
.venv\Scripts\python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

This creates `dist\ThaiMeetingTranslator.exe`, a single file of about
30 MB. Copy it to any folder, and put your `.env` in the **same folder**. The
app keeps `transcripts\`, `logs\` and `usage.json` there too. It takes a few
seconds to start, because it unpacks itself first. Windows SmartScreen may
warn about an unsigned app: choose *More info → Run anyway*.

## 4. Recommended audio setup (Teams / Zoom / Meet)

The main risk is a **feedback loop**: if the Thai voice plays on the device
being captured, the app hears its own Thai voice and translates it again.

**Best: two separate output devices**

| | Device |
|---|---|
| Meeting app speaker (Teams/Zoom setting) | Device A, e.g. laptop speakers or monitor audio |
| App "Input device" | **Device A [Loopback]** |
| App "Thai voice output" | **Device B**, e.g. USB/Bluetooth headphones |

If you only want to hear Thai, install a free virtual audio cable (e.g.
VB-Audio Virtual Cable). Set the meeting's speaker to *CABLE Input*, capture
*CABLE Input [Loopback]* in this app, and send the Thai voice to your
headphones.

**Only one output device?** Do one of these:

- Turn on **Voice off** (Ctrl+M) and just read the captions. This also saves TTS
  characters, and the mic is then never paused.
- Turn on **Pause listening while the voice speaks**. The app sends silence to Azure
  while the Thai voice plays (plus 0.4 s of echo tail). English spoken during
  that time is **missed**.

The app shows a red warning when the voice output and the captured loopback
device are the same.

**Microphone mode (in-room):** play the Thai voice on headphones, or keep
*Pause listening while the voice speaks* on (it switches on by itself when
you choose Microphone). Otherwise the mic picks up the Thai voice. Also keep your microphone **muted in Teams/Zoom** if the Thai voice
plays on speakers, or the other participants will hear it.

## 4b. Onsite conversation (two people, one device)

Choose **Onsite** in the pill switch. The app then uses the microphone and
pauses it only while the voice speaks.

- **Auto · อัตโนมัติ [F4]** (the default): just talk. The mic stays open for
  both people and the detected language of each sentence decides the
  direction and the voice (English → Thai voice, Thai → English voice), with
  no reconnect between speakers.
- **English · อังกฤษ [F1]** / **ไทย · Thai [F2]** locks the direction to that
  person, if detection picks the wrong language (e.g. lots of English words
  in Thai). **Auto-switch turns** then passes the floor to the other person
  once the translation has been read.
- **Banner:** green = speak now · blue = switching turn (you can already
  start speaking; that audio is buffered) · amber = translating · red =
  reading the translation, please wait.
- **Feedback protection:** while the voice plays, and for 0.7 s after it
  (`ONSITE_TAIL_S`), the microphone is replaced with silence. A sentence that
  still matches something the voice just said is ignored as the app hearing
  itself, and noted in the history.
- **Skip [Esc]** cuts off the sentence being read; the next one, if
  any, still plays. **Replay [F3]** reads the last translation again (see
  *Replay* below).

Limits: anything said while the banner is red is **not heard** (unless the
voice is off). This is gating, not echo cancellation. With a locked side,
each turn change reconnects the recognizer (the free tier allows one
connection); the previous speaker's last sentence is still finished and the
next speaker's audio is buffered, so nothing is lost, but the first
translation of a turn comes a little later. Auto needs no reconnects.

## 5. Transcripts

- Each finished sentence is saved immediately to
  `transcripts/meeting_YYYYMMDD_HHMMSS.txt` and `.srt`, so a crash doesn't
  lose the meeting.
- **⤓ Export** (top bar) saves a `.txt` or `.srt` copy wherever you choose.
- Files are UTF-8 **with BOM** and CRLF line endings, so Thai shows correctly
  in Notepad, Word, Excel and video players.
- Each `.txt` entry is marked `EN -> TH` or `TH -> EN`. Each SRT cue has the
  translation on top and the original below. Times are measured from when
  you pressed Start.

## 6. How it behaves when things go wrong

| Situation | What you see |
|---|---|
| Missing `.env` values | Error right away: values not set |
| Wrong key | *Azure rejected the key…*, with **no reconnect** |
| Wrong region | *Region 'xyz' was not found…*, with **no reconnect** |
| AuthenticationFailure / BadRequest / Forbidden from the SDK | Clear error, no reconnect |
| Network drop, service timeout or session ended by Azure, during a working session | *Reconnecting in Ns (attempt k)…* with backoff of 1, 2, 4, 8, 15, then every 30 s. It **never gives up** on its own; Stop ends it. Subtitles, history, voice and mute state are kept. When it is back, a note in the conversation says how long it was away |
| Speech during an outage | The last 20 s (`RECONNECT_BUFFER_S`) are buffered and translated once it is back; the note says if more was lost |
| Stop pressed while reconnecting | Stops at once; no new connection opens |
| Stop pressed while still starting (key check, opening the device, connecting) | Start is abandoned; anything already opened is closed; nothing connects afterwards |
| Stop pressed while the voice is speaking | The voice cuts off within ~0.1 s; queued sentences are dropped; nothing plays after Stop |
| Double-click Start / Stop | The second click is ignored |

Each drop is counted once, even though the SDK reports both *canceled* and
*session stopped* for the same drop. A session that stays connected for 60 s
resets the backoff. To give up after N attempts instead, set
`MAX_RECONNECT_ATTEMPTS = N` in `translator/config.py`. Before connecting, the app checks the key and
region against Azure's token endpoint. Without that check, a wrong region
looks exactly like a network failure in the SDK.

**When speech comes faster than the voice can read it** (queue policy):

| Setting | Policy | What happens |
|---|---|---|
| Meeting | **Catch up** | At most 2 sentences wait. Beyond that the **oldest** unspoken ones are skipped, so the voice stays current. Skipped sentences stay in captions, history and transcript, and can be replayed. The status bar shows *Voice queue: N (skipped M)*. |
| Onsite | **Every sentence, in order** | Nothing is skipped: in a conversation a missed number or condition matters. Waiting sentences in the same language are **merged** into one synthesis request (up to 400 characters), which drains a backlog faster and uses fewer of the 20 requests a minute. If the voice still falls more than 15 s behind, the status bar turns red: *voice is ~N s behind*. |

Sentences are read one at a time and never overlap.

**Replay:** click **▶** on any message card to hear that translation again,
or press **F3** for the last one. A replay plays right after
the sentence currently being read (it doesn't cut it off), ahead of anything
waiting. It is never skipped or merged. With the voice off it is refused:
off means no audio at all, and the mic isn't paused then. The last 50 spoken translations are kept in
memory, so replaying them is instant and uses no TTS characters. Older ones,
or ones that were skipped, are synthesized again. Replay works while the
translator is running. In onsite mode the microphone is muted during a
replay, as for any voice output.

## 6b. Long sessions (lectures, presentations, all-day meetings)

Press the mic once; it stays open until you press it again. Recognition is
continuous (`start_continuous_recognition_async`): nothing stops after a
sentence or after silence, and no Start/Stop is needed between sentences.

- **Live subtitles:** the partial sentence shows as a **Live** card (and in
  the subtitle window) and is replaced by the final text and translation.
- **Voice off (subtitles only), Ctrl+M:** the sentence being read stops at
  once, the queue is cleared, nothing new is queued, and the mic is never
  paused (100 % capture). Recognition, translation and subtitles carry on.
  **Voice on** speaks only sentences from then on, not the backlog. The
  choice stays for the session (and the next one).
- **Voice on with a mic:** feedback protection stays on: the mic is replaced
  by silence while the voice plays plus a short tail, and a result that
  repeats what the voice just said is dropped. The recognizer keeps running
  through it; nothing is reset.
- **Subtitle window (F11):** dark, very large, bottom-anchored subtitles for
  a projector. Drag it to the projector screen, then F11 (or double-click)
  for full screen. A−/A+ size, *Original* shows or hides the source line,
  Esc leaves full screen.
- **Memory:** the conversation draws the newest 200 cards; older ones are
  dropped from the screen in batches while you follow the newest line
  (*Show earlier* brings them back). Every sentence stays in memory for
  replay and in the transcript file. The voice cache (50), latency rows in
  memory (500), recognizer records and the audio buffer are all capped.

**Soak test** (real Azure, mic and speaker; needs `.env`): a voice reads the
test phrases on the speaker, over and over, and the app's pipeline listens
through the mic. It reports missed, duplicated and late sentences, outages,
audio that played while muted, latency (first vs last third of the run),
and memory/CPU over time, in `results/soak-…/summary.md`.

```powershell
python -m tools.soak --minutes 30                         # meeting, auto-detect, voice on
python -m tools.soak --minutes 30 --setting onsite        # onsite settings
python -m tools.soak --minutes 30 --mute                  # subtitles only
python -m tools.soak --minutes 30 --mute-every 5 --drop-every 10   # toggle voice, force reconnects
python -m tools.soak --minutes 30 --pace lecture --mute   # back-to-back speech
python -m tools.soak --offline --minutes 120 --speed 40   # no Azure: the window, 2 h simulated
```

A 30-minute live run uses about 30 of the 300 free translation minutes a
month, and with the voice on about 15–20k of the 500k free TTS characters.

## 7. Verification checklist

Automated tests (offline except the two preflight tests, which use a fake key):

```powershell
python -m unittest discover -s tests -v
```

Manual checks:

- [ ] Put a wrong key in `.env`, then Start. You get an immediate key error, not "Reconnecting". Repeat with a wrong region.
- [ ] Mic mode: speak English. A Thai caption appears and NiwatNeural speaks it.
- [ ] Switch to Thai → English and speak Thai. An English caption appears and JennyNeural speaks it.
- [ ] Press Stop while the voice is speaking. It stops at once and nothing more plays.
- [ ] Onsite, laptop speakers (no headphones): have a two-way conversation for 5 minutes. The app never translates its own voice, and no sentence spoken while the banner was green is missing from the conversation.
- [ ] Click ▶ on an earlier message card: that translation is read again, after the current one, and the card is highlighted while it plays.
- [ ] Onsite: finish a sentence and press the other side's button immediately. The last sentence is still translated, and the other person's first words are too.
- [ ] Loopback mode with the voice on headphones: play an English video. It translates, and the Thai voice is not translated again (no Thai-sounding English nonsense in the conversation).
- [ ] UI states (see *Interface states* below): idle, connecting, listening, speaking with the muted-mic badge, the voice picker, and the microphone-blocked dialog.
- [ ] Run for 15 minutes: no "Reconnecting" without a real network drop, and the voice queue stays at 0–2.
- [ ] Open the exported `.txt` in Notepad and the `.srt` in VLC or Notepad. The Thai text shows correctly.
- [ ] `python -m tools.measure --source speaker` and `--source human` (auto), then `--mode floor`: read `summary.md` (section 8).
- [ ] Long sessions: `python -m tools.soak --minutes 30` in both settings, voice on and `--mute`, once with `--mute-every 5 --drop-every 10`: no missed or duplicated phrases beyond a few, every outage recovered, no audio while muted, memory flat (section 6b).

### Interface states

Screenshots of each state are in [`docs/screenshots/`](docs/screenshots/)
(desktop, 768 px, 375 px and dark). To see each state without a key or a
meeting:

| State | How to get there |
|---|---|
| Idle | Start the app. Red mic button, *Tap the mic to start translating* bubble gently pulsing. |
| Connecting | Press the mic. Spinner around a dimmed button; clicks are ignored until it is up. |
| Error | Press the mic without a `.env`. Red bubble *Couldn't start* and the reason below the button. |
| Listening / translating | With a key: speak. The ring grows with your voice; the **Live** card shows the partial translation. |
| Speaking | The button turns blue with a speaker. With *Pause listening while the voice speaks* on (always on in Onsite), a crossed-out mic badge shows the mic is paused. |
| Voice picker | Audio and voice → **Select a voice**. Search, *All / Female / Male*, English and Thai tabs with counts, ▶ preview. Esc, ×, or a click outside closes it. |
| Microphone blocked | Windows Settings → Privacy & security → Microphone → turn off *Let desktop apps access your microphone*, choose **Microphone**, press the mic. |

## 8. Measuring accuracy and latency

### In the app (every session)

Every session writes `logs/latency-YYYYMMDD-HHMMSS.jsonl` with one line per
translated sentence. To protect meeting content, the line holds only timings,
languages, lengths and the outcome, not the text. The status bar shows the
latest result: *last delay after speech: text 0.9 s, voice 1.6 s*.
`logs/translator.log` holds the same as one readable line per sentence.

| Stage | Measured from → to |
|---|---|
| `first_interim_s` | speech starts → first partial caption |
| `recognize_s` | speech ends → final text from Azure (end-of-speech detection + translation) |
| `ui_s` | final text → shown on screen |
| `caption_s` | **speech ends → translated text on screen** |
| `queue_s` | shown → TTS request sent (waiting behind earlier sentences, and the F0 rate limit, shown separately as `rate_wait_s`) |
| `synth_s` | TTS request → audio received (Azure synthesizes the whole sentence first) |
| `play_s` | audio received → sound starts (includes the output latency PortAudio reports) |
| `total_s` | **speech ends → translated voice starts** |

"Speech ends" is when the last word was *captured*. The app gets it from the
audio offset in Azure's result, mapped back to the capture time of that
audio. Audio that was buffered during a turn switch is therefore timed
correctly, and Azure's own delay isn't hidden.

Summary of all sessions so far (median, p90 and max per stage, overall and
per direction):

```powershell
python -m tools.latency_report
```

### Measurement tool (detection accuracy + latency)

`tools/measure.py` runs the app's own pipeline without the window. It
presents each phrase in `tools/phrases.tsv`: normal sentences, short replies,
numbers, names, and Thai with English words, alternating between languages.
For each phrase it waits for the translation to be spoken, then scores which
language was detected.

```powershell
python -m tools.measure --list-devices
python -m tools.measure --source speaker               # a TTS voice reads each phrase aloud; the mic listens
python -m tools.measure --source human                 # you read each phrase aloud
python -m tools.measure --source synthetic             # audio pushed straight in: Azure only, no room
python -m tools.measure --source speaker --mode floor  # direction set per phrase, like the onsite buttons
#   --mic "Realtek"  --out "Speakers"  --limit 10  --silence-ms 500  --no-voice
```

Results go to `results/measure-<time>-<source>-<mode>/`:

- `summary.md`: detection accuracy (by language and by category, with each wrong or missed phrase), recognition text similarity, and the latency table per stage
- `phrases.csv`: one row per phrase (opens in Excel)
- `rows.jsonl`: one row per sentence, including the text
- `run.log`

With `speaker` and `synthetic` the true end of each phrase is known.
`summary.md` compares it with the recognizer's speech end as a sanity check.
The voices that read the prompts (Guy, Premwadee) differ from the
translation voices. The prompts are synthesized once and cached in
`results/.prompt-cache/`.

Cost per full run of 30 phrases: about 5 minutes of translation audio (of
the 5 free hours a month) and about 2,000 TTS characters. The tool keeps
within the 20 requests a minute.

**End-of-speech silence.** Azure waits for a pause before it finalizes a
sentence, and that pause is part of `recognize_s`. Set
`SEGMENTATION_SILENCE_MS` in `.env` (100–5000), or pass `--silence-ms`, to
try a shorter one. A shorter pause gives earlier translations, but a
sentence with a long pause in it gets split in two.

## Files

```
main.py                 entry point
translator/config.py    settings, F0 limits, .env loading, key/region preflight
translator/audio.py     WASAPI device list, mic/loopback capture, resampling, playback
translator/recognition.py  TranslationRecognizer, direction/auto-detect, error classification, reconnect
translator/session.py   start/stop state machine and per-session resource ownership
translator/onsite.py    onsite turn-taking, phases, echo filter
translator/tts.py       TTS queue (voice per language, queue policies, replay + audio cache, F0 rate limit)
translator/latency.py   per-stage latency: stream clock, per-sentence log, stage tables
translator/transcript.py   auto-save and .txt/.srt export
translator/usage.py     session and monthly usage counter
translator/ui.py        the window: wires the components to the session logic
translator/subtitle_view.py  projector subtitle window
translator/theme.py     design tokens (colours, radii, shadows, motion, fonts), light/dark
translator/widgets.py   Button, IconButton, PillTabs, PillSelect, Toggle, Modal, RecordButton, ...
translator/messages.py  conversation list (message cards, live card, jump to latest)
translator/voice_picker.py  voice picker dialog
translator/voices.py    Azure voice list (cached), saved voice choice, preview
translator/winmic.py    Windows microphone privacy check
translator/paint.py     anti-aliased shapes for the canvas (Pillow)
translator/assets/fonts Plus Jakarta Sans + Noto Sans Thai (SIL Open Font License)
tools/measure.py        detection accuracy + latency measurement with real Azure/mic/speaker
tools/phrases.tsv       test phrases for it
tools/latency_report.py summary of the app's logs/latency-*.jsonl
tools/soak.py           long-session soak test (live, or offline accelerated UI)
tests/                  unit tests (core, lifecycle, bidirectional, onsite, queue/replay, latency, measure, UI components, long-form)
```

## License

[MIT](LICENSE)
