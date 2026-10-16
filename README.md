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

## 3. Run

```powershell
.\.venv\Scripts\activate
python main.py
```

1. **Listen to:** pick *System audio* (Teams/Zoom/Meet on this PC) or
   *Microphone* (people speaking in the room).
2. **Input device:** for system audio, pick the `[Loopback]` entry of the
   device the meeting plays on.
3. **Voice output:** pick where the voice should play, ideally headphones.
4. **Direction:** *English → Thai*, *Thai → English*, or *Auto-detect
   English/Thai (experimental)*. You can change it while running. The
   recognizer reconnects, so speech during the switch (about a second) is
   missed.
5. Press **Start**. The status line shows *Checking Azure key and region…*,
   then *Listening (English → Thai)*.

The big line in the caption area is the translation. The smaller line below
it is what was said. Grey text is the live (interim) guess. It turns black
when the sentence is final, and the final sentence is added to the history
and spoken in the voice for its target language.

**Auto-detect** asks Azure to decide per sentence whether English or Thai was
spoken (continuous language identification). Accuracy and delay have not
been measured with real speech yet. Use a fixed direction for anything
important until they have.

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

- Tick **Mute voice** and just read the captions. This also saves TTS characters.
- Tick **Pause listening while voice speaks**. The app sends silence to Azure
  while the Thai voice plays (plus 0.4 s of echo tail). English spoken during
  that time is **missed**.

The app shows a red warning when the voice output and the captured loopback
device are the same.

**Microphone mode (in-room):** play the Thai voice on headphones, or tick
*Pause listening while voice speaks*. Otherwise the mic picks up the Thai
voice. Also keep your microphone **muted in Teams/Zoom** if the Thai voice
plays on speakers, or the other participants will hear it.

## 4b. Onsite conversation (two people, one device)

Choose **Setting: Onsite conversation**. The app then uses the microphone,
always mutes it while the voice speaks, and lets one side talk at a time.

- **Whose turn:** the big buttons **English speaker [F1]** and
  **ผู้พูดภาษาไทย [F2]** give the floor to that person. Only their language is
  recognized and translated (English speaker → Thai voice, Thai speaker →
  English voice). The Direction row follows the floor and can't be set by
  hand.
- **Auto-switch turns** (on by default): once the translation has been read
  out, the floor passes to the other person. Press your own button again to
  keep talking.
- **Banner:** green = speak now · blue = switching turn (you can already
  start speaking; that audio is buffered) · amber = translating · red =
  reading the translation, please wait.
- **Feedback protection:** while the voice plays, and for 0.7 s after it
  (`ONSITE_TAIL_S`), the microphone is replaced with silence. A sentence that
  still matches something the voice just said is ignored as the app hearing
  itself, and noted in the history.
- **Skip voice [Esc]** cuts off the sentence being read; the next one, if
  any, still plays. **Replay [F3]** reads the last translation again (see
  *Replay* below).

Limits: anything said while the banner is red is **not heard**. This is
turn-taking, not echo cancellation. Each turn change reconnects the
recognizer (the free tier allows one connection). The previous speaker's last
sentence is still finished and the next speaker's audio is buffered, so
nothing is lost, but the first translation of a turn comes a little later.

## 5. Transcripts

- Each finished sentence is saved immediately to
  `transcripts/meeting_YYYYMMDD_HHMMSS.txt` and `.srt`, so a crash doesn't
  lose the meeting.
- **Export .txt / Export .srt** saves a copy wherever you choose.
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
| Network drop during a working session | *Reconnecting in Ns (attempt k/5)…*, with backoff of 1, 2, 4, 8 and 15 s |
| 5 failed reconnects | *Gave up… Press Start to try again* |
| Stop pressed while reconnecting | Stops at once; no new connection opens |
| Stop pressed while still starting (key check, opening the device, connecting) | Start is abandoned; anything already opened is closed; nothing connects afterwards |
| Stop pressed while the voice is speaking | The voice cuts off within ~0.1 s; queued sentences are dropped; nothing plays after Stop |
| Double-click Start / Stop | The second click is ignored |

Each drop is counted once, even though the SDK reports both *canceled* and
*session stopped* for the same drop. A session that stays connected for 60 s
resets the retry counter. Before connecting, the app checks the key and
region against Azure's token endpoint. Without that check, a wrong region
looks exactly like a network failure in the SDK.

**When speech comes faster than the voice can read it** (queue policy):

| Setting | Policy | What happens |
|---|---|---|
| Meeting | **Catch up** | At most 2 sentences wait. Beyond that the **oldest** unspoken ones are skipped, so the voice stays current. Skipped sentences stay in captions, history and transcript, and can be replayed. The status bar shows *Voice queue: N (skipped M)*. |
| Onsite | **Every sentence, in order** | Nothing is skipped: in a conversation a missed number or condition matters. Waiting sentences in the same language are **merged** into one synthesis request (up to 400 characters), which drains a backlog faster and uses fewer of the 20 requests a minute. If the voice still falls more than 15 s behind, the status bar turns red: *voice is ~N s behind*. |

Sentences are read one at a time and never overlap.

**Replay:** click **▶** at the start of any line in History to hear that
translation again, or press **Replay last [F3]**. A replay plays right after
the sentence currently being read (it doesn't cut it off), ahead of anything
waiting. It is never skipped or merged, and plays even with *Mute voice* on,
because you asked for it. The last 50 spoken translations are kept in
memory, so replaying them is instant and uses no TTS characters. Older ones,
or ones that were skipped, are synthesized again. Replay works while the
translator is running. In onsite mode the microphone is muted during a
replay, as for any voice output.

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
- [ ] Onsite, laptop speakers (no headphones): have a two-way conversation for 5 minutes. The app never translates its own voice, and no sentence spoken while the banner was green is missing from the history.
- [ ] Click ▶ on an earlier History line: that translation is read again, after the current one.
- [ ] Onsite: finish a sentence and press the other side's button immediately. The last sentence is still translated, and the other person's first words are too.
- [ ] Loopback mode with the voice on headphones: play an English video. It translates, and the Thai voice is not translated again (no Thai-sounding English nonsense in the history).
- [ ] Run for 15 minutes: no "Reconnecting" without a real network drop, and the voice queue stays at 0–2.
- [ ] Open the exported `.txt` in Notepad and the `.srt` in VLC or Notepad. The Thai text shows correctly.
- [ ] `python -m tools.measure --source speaker` and `--source human` (auto), then `--mode floor`: read `summary.md` (section 8).

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
translator/ui.py        Tkinter window
tools/measure.py        detection accuracy + latency measurement with real Azure/mic/speaker
tools/phrases.tsv       test phrases for it
tools/latency_report.py summary of the app's logs/latency-*.jsonl
tests/                  unit tests (core, lifecycle, bidirectional, onsite, queue/replay, latency, measure)
```

## License

[MIT](LICENSE)
