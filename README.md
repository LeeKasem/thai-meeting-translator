# Meeting Translator: English → Thai (Azure Speech)

A Windows desktop app that listens to an English meeting, shows live Thai and
English captions, and reads the Thai aloud with the neural voice
**th-TH-NiwatNeural**.

```
Meeting audio (mic or system loopback)
  → 16 kHz mono PCM → Azure TranslationRecognizer (en-US → th)
  → Thai caption + history + auto-saved transcript
  → Thai TTS queue (th-TH-NiwatNeural) → your chosen output device
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

## 3. Run

```powershell
.\.venv\Scripts\activate
python main.py
```

1. **Listen to:** pick *System audio* (Teams/Zoom/Meet on this PC) or
   *Microphone* (people speaking in the room).
2. **Input device:** for system audio, pick the `[Loopback]` entry of the
   device the meeting plays on.
3. **Thai voice output:** pick where the Thai voice should play, ideally headphones.
4. Press **Start**. The status line shows *Checking Azure key and region…*,
   then *Listening*.

Grey text in the caption area is the live (interim) guess. It turns black
when the sentence is final, and the final sentence is added to the history
and spoken.

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

## 5. Transcripts

- Each finished sentence is saved immediately to
  `transcripts/meeting_YYYYMMDD_HHMMSS.txt` and `.srt`, so a crash doesn't
  lose the meeting.
- **Export .txt / Export .srt** saves a copy wherever you choose.
- Files are UTF-8 **with BOM** and CRLF line endings, so Thai shows correctly
  in Notepad, Word, Excel and video players.
- Each SRT cue has the Thai line on top and the English line below. Times are
  measured from when you pressed Start.

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
| Double-click Start | The button is disabled on the first click |

Each drop is counted once, even though the SDK reports both *canceled* and
*session stopped* for the same drop. A session that stays connected for 60 s
resets the retry counter. Before connecting, the app checks the key and
region against Azure's token endpoint. Without that check, a wrong region
looks exactly like a network failure in the SDK.

**Thai voice falling behind:** sentences are spoken one at a time. If more
than 2 are waiting, the oldest unspoken ones are skipped. They stay in the
captions and transcript. The status bar shows *Voice queue: N (skipped M)*.

## 7. Verification checklist

Automated tests (offline except the two preflight tests, which use a fake key):

```powershell
python -m unittest discover -s tests -v
```

Manual checks:

- [ ] Put a wrong key in `.env`, then Start. You get an immediate key error, not "Reconnecting". Repeat with a wrong region.
- [ ] Mic mode: speak English. A Thai caption appears and NiwatNeural speaks it.
- [ ] Loopback mode with the voice on headphones: play an English video. It translates, and the Thai voice is not translated again (no Thai-sounding English nonsense in the history).
- [ ] Run for 15 minutes: no "Reconnecting" without a real network drop, and the voice queue stays at 0–2.
- [ ] Open the exported `.txt` in Notepad and the `.srt` in VLC or Notepad. The Thai text shows correctly.

## Files

```
main.py                 entry point
translator/config.py    settings, F0 limits, .env loading, key/region preflight
translator/audio.py     WASAPI device list, mic/loopback capture, resampling, playback
translator/recognition.py  TranslationRecognizer, error classification, reconnect
translator/tts.py       Thai TTS queue (no overlap, backlog skipping, F0 rate limit)
translator/transcript.py   auto-save and .txt/.srt export
translator/usage.py     session and monthly usage counter
translator/ui.py        Tkinter window
tests/test_core.py      unit tests
```
