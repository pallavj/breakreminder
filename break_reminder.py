#!/usr/bin/env python3
"""
break_reminder.py  —  Because your glutes didn't sign up for this.

Architecture:
  • Runs as a persistent background daemon (kept alive by launchd)
  • Every 45-60 min: picks a random message + exercise → POSTs to ntfy.sh
  • Notification has three action buttons:  ✅ Done  |  ⏰ 15 min  |  ⏰ 30 min
  • Those buttons POST back to a "control" ntfy topic
  • This daemon subscribes to the control topic via SSE and adjusts the timer live
  • Streak + total-break count are persisted to ~/.break_reminder.json
"""

import urllib.request
import urllib.error
import json
import random
import time
import threading
import os
import sys
import logging
from datetime import datetime, timedelta

# ─── Tunables ──────────────────────────────────────────────────────────────────
MIN_INTERVAL_SEC = 45 * 60   # 45 minutes
MAX_INTERVAL_SEC = 60 * 60   # 60 minutes
NTFY_BASE        = "https://ntfy.sh"
QUIET_START      = 1         # 1 AM  — no notifications
QUIET_END        = 10        # 10 AM — resume
CONFIG_FILE      = os.path.expanduser("~/.break_reminder.json")
LOG_FILE         = os.path.expanduser("~/Library/Logs/break_reminder.log")
PID_FILE         = os.path.expanduser("~/.break_reminder.pid")

# ─── Message Bank (44 entries) ─────────────────────────────────────────────────
REMINDERS = [
    ("🪑 Your Chair Called",          "It says you've worn a permanent groove into it. Time to betray the chair."),
    ("📊 Sitting Audit Complete",     "Results: too much. Recommendation: move. Confidence: very high."),
    ("🦴 Bone Report",                "Your skeleton has filed a formal complaint. Reference ID: GET-UP-NOW."),
    ("🧠 Brain Boost Incoming",       "2 minutes of movement increases blood flow to your brain by 40%. Be smarter. Stand up."),
    ("🌿 Your Future Self Texted",    "They said 'seriously, just stand up.' Also 'why didn't you take care of your knees?'"),
    ("🚨 Stiffness Alert",            "Level: Moderate. Threat: Real. Countermeasure: Squat. Execute now."),
    ("🎯 Daily Quest Available",      "New objective unlocked: 'Stop being a human statue for 2 minutes.' Reward: endorphins."),
    ("🤖 Robot Check",                "Are you a robot? Robots don't need breaks. If you're human, prove it. Move."),
    ("💡 Fun Fact",                   "Your body burns more calories standing than sitting. Brought to you by: gravity."),
    ("📡 Transmission from Your Hips","Signal strength: weak. Message: 'PLEASE MOVE US.' Signed, Your Hip Flexors."),
    ("🎵 Intermission",               "Even musicians take a break between sets. You're the headliner. Don't skip intermission."),
    ("🧘 Energy Hack",                "10 squats will wake you up faster than a second coffee. And your kidneys will thank you."),
    ("🦴 Spine Update",               "Current status: compressing. Preferred status: decompressing. Required action: stand."),
    ("🔋 Recharge Required",          "Your body's battery is draining. Moving is the charger. No USB cable needed."),
    ("📬 You've Got Legs",            "They've been very patient. It would be rude not to use them."),
    ("🦁 Wildlife Reminder",          "Lions don't sit for hours. Channel your inner lion. Roar optional."),
    ("🎰 Jackpot Move",               "Every break = investing in sharper focus for the next hour. ROI: excellent."),
    ("🌈 Something Good Is Coming",   "It's the feeling after 10 squats. You know the one. Go get it."),
    ("⚡ Power Move",                  "The most productive people take regular breaks. This is your cue."),
    ("🎓 Science Corner",             "Micro-breaks reduce decision fatigue. Squats make you smarter. You're welcome."),
    ("🛸 Urgent Transmission",        "This is your body calling from inside the chair. Requesting immediate extraction."),
    ("🥊 Fight Club Rule",            "First rule: you do NOT skip your break. Second rule: YOU DO NOT SKIP YOUR BREAK."),
    ("🌻 Good News",                  "You're not stuck. The chair has no lock. You can leave anytime. The time is now."),
    ("🗺️ Adventure Awaits",           "Just past the edge of your desk. 2 minutes of walking. Unmapped territory."),
    ("🎗️ Reminder from Past You",     "Past you said 'I'll definitely take breaks.' Present you: make past you proud."),
    ("🚀 Launch Sequence",            "T-minus zero. Ignition: your legs. Mission: not sitting for 2 minutes. Launch."),
    ("🐢 Don't Be a Turtle",          "Turtles are great. But even turtles move around. Move."),
    ("🎭 Plot Twist",                 "You were sitting the whole time. The real work is getting up. The end. Go."),
    ("🍵 Tea Time",                   "It's break o'clock somewhere. Walk to the kitchen. Make something warm. Be human."),
    ("🧩 Missing Piece",              "Your workflow has everything except one thing: movement. Add it now. Puzzle complete."),
    ("🎈 Celebration Incoming",       "Post-squat endorphins are waiting for you at the squat zone. RSVP: immediately."),
    ("🦅 Soar, Don't Slouch",         "Eagles don't slouch. Spread your wings. Or just your legs. Walk."),
    ("🎯 Target Acquired",            "Target: the floor. Method: squats. Objective: not being a desk ornament."),
    ("🌊 Tide Turning",               "For the worse, if you keep sitting. Get up. The tide turns now."),
    ("🎪 Two Minutes",                "Presenting… the break you absolutely have time for. Two minutes. That's it."),
    ("💼 Not a Drill",                "Well, it kind of is. A drill for your cardiovascular system. Take it seriously."),
    ("🎬 Scene Break",                "Every great story has a pause. This is yours. Make it physical."),
    ("🏆 Streak Protector",           "One small break now = one big streak later. Don't let it die here."),
    ("🌙 Future You Thanks You",      "Every break today is an investment in energy tonight. You know this. Do it."),
    ("🍕 Pizza Theory",               "You wouldn't eat pizza for 60 min without water. Don't sit without a break."),
    ("🧊 Heat It Up",                 "Your circulation is cooling from inactivity. Fire it up. Squats. Now. Go."),
    ("💬 Your Back Specifically",     "Your lower back left a voicemail. It's not angry. Just disappointed."),
    ("🎻 Dramatic Pause",             "Even the most gripping symphony has a rest. Consider this yours."),
    ("🌍 Global Context",             "Somewhere out there, someone just did 10 squats and felt amazing. Be that person."),
]

EXERCISES = [
    "10 squats 🦵",
    "Walk around for 2 minutes 🚶",
    "15 jumping jacks ⚡",
    "10 calf raises 🦶",
    "5 pushups (or try) 💪",
    "30-second wall sit 🏋️",
    "Walk to get a glass of water 💧",
    "Neck rolls + shoulder shrugs 🔄",
    "10 walking lunges 🧎",
    "Stand and stretch for 2 minutes 🧍",
    "10 desk push-ups 📐",
    "2-min dance break (no witnesses) 🕺",
    "March in place for 60 seconds 🥁",
    "10 hip circles each direction 🔁",
    "Walk outside for 2 min 🌳",
    "10 standing side leg raises 🦵",
    "Slow deep breathing walk 🌬️",
    "Wrist + ankle circles x10 🔄",
]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    config = {
        "topic":         "pallav-moves-56f0f50a",
        "control_topic": "pallav-ctrl-56f0f50a",
        "streak":        0,
        "total_breaks":  0,
    }
    save_config(config)
    return config


def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not save config: {e}")


def ntfy_post(topic, title, body, tags="", actions_str=""):
    """
    Post to ntfy using JSON body so emoji titles/tags work without
    header charset issues (Python's http.client only allows latin-1 in headers).
    """
    url = f"{NTFY_BASE}"

    payload: dict = {
        "topic":    topic,
        "title":    title,
        "message":  body,
        "priority": 5,
    }

    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",")]

    if actions_str:
        # Parse our semicolon-separated shorthand into ntfy JSON action objects
        acts = []
        for part in actions_str.split(";"):
            parts = [p.strip() for p in part.strip().split(",")]
            # parts: [type, label, url, key=val, ...]
            if len(parts) < 3:
                continue
            act: dict = {"action": parts[0], "label": parts[1], "url": parts[2]}
            for kv in parts[3:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    act[k.strip()] = v.strip()
            acts.append(act)
        if acts:
            payload["actions"] = acts

    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        logging.error(f"ntfy POST failed: {e}")
        return False


# ─── Main Daemon ───────────────────────────────────────────────────────────────

class BreakReminder:
    def __init__(self, config):
        self.config  = config
        self.topic   = config["topic"]
        self.ctrl    = config["control_topic"]
        self.streak  = config.get("streak", 0)
        self.total   = config.get("total_breaks", 0)
        self._lock   = threading.Lock()
        self._next   = time.time() + random.randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)

    # ── Sending ────────────────────────────────────────────────────────────────

    def _fire(self):
        title, msg = random.choice(REMINDERS)
        exercise   = random.choice(EXERCISES)

        streak_line = ""
        if self.streak >= 5:
            streak_line = f"\n\n🔥 {self.streak}-break streak! You're on fire!"
        elif self.streak >= 2:
            streak_line = f"\n\n🔥 {self.streak} in a row! Don't stop now."

        body = f"{msg}\n\nMove: {exercise}{streak_line}"

        actions = (
            f"http, ✅ Done!, {NTFY_BASE}/{self.ctrl}, method=POST, body=done; "
            f"http, 🏃 Working Out, {NTFY_BASE}/{self.ctrl}, method=POST, body=working_out; "
            f"http, ⏰ 15 min, {NTFY_BASE}/{self.ctrl}, method=POST, body=snooze15"
        )

        logging.info(f"Firing reminder | streak={self.streak} total={self.total} | '{title}'")
        ntfy_post(self.topic, title, body, tags="runner,alarm_clock", actions_str=actions)

        # Default next reminder (overridden if user taps Done/Snooze)
        with self._lock:
            self._next = time.time() + random.randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)

    # ── Control topic listener ──────────────────────────────────────────────────

    def _on_control(self, message: str):
        with self._lock:
            if message == "done":
                self.streak += 1
                self.total  += 1
                self.config["streak"]       = self.streak
                self.config["total_breaks"] = self.total
                save_config(self.config)
                self._next = time.time() + random.randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)
                logging.info(f"✅ Done tapped | streak={self.streak} total={self.total}")
                # Send a little confirmation back
                ntfy_post(
                    self.topic,
                    f"🔥 Streak: {self.streak}  |  Total: {self.total}",
                    "Nice work! See you in ~50 min. 💪",
                    tags="white_check_mark",
                )
            elif message == "working_out":
                self.streak += 1
                self.total  += 1
                self.config["streak"]       = self.streak
                self.config["total_breaks"] = self.total
                save_config(self.config)
                self._next = time.time() + random.randint(MIN_INTERVAL_SEC, MAX_INTERVAL_SEC)
                logging.info(f"🏃 Working Out tapped | streak={self.streak} total={self.total}")
                ntfy_post(
                    self.topic,
                    f"🏃 Already on it — Streak: {self.streak}  |  Total: {self.total}",
                    "Look at you go. See you in ~50 min. 🔥",
                    tags="muscle",
                )
            elif message == "snooze15":
                self._next = time.time() + 15 * 60
                logging.info("⏰ Snoozed 15 min")

    def _listen_control(self):
        """Subscribe to control topic via SSE — reconnects on any error."""
        url = f"{NTFY_BASE}/{self.ctrl}/sse"
        while True:
            try:
                logging.info(f"Connecting to control SSE: {self.ctrl}")
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    buf = ""
                    while True:
                        chunk = resp.read(512)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if line.startswith("data:"):
                                try:
                                    data = json.loads(line[5:].strip())
                                    if data.get("event") == "message":
                                        self._on_control(data.get("message", ""))
                                except json.JSONDecodeError:
                                    pass
            except Exception as e:
                logging.warning(f"Control SSE disconnected ({e}), reconnecting in 15s…")
                time.sleep(15)

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _in_quiet_hours(self):
        hour = datetime.now().hour
        return QUIET_START <= hour < QUIET_END

    def _next_wake_time(self):
        """Return the datetime for QUIET_END today, or tomorrow if already past."""
        now  = datetime.now()
        wake = now.replace(hour=QUIET_END, minute=0, second=0, microsecond=0)
        if wake <= now:
            wake += timedelta(days=1)
        return wake

    def run(self):
        logging.info(f"Break Reminder started")
        logging.info(f"  ntfy topic   : {self.topic}")
        logging.info(f"  control topic: {self.ctrl}")
        mins = max(0, (self._next - time.time())) / 60
        logging.info(f"  first reminder in ~{mins:.0f} min")

        t = threading.Thread(target=self._listen_control, daemon=True)
        t.start()

        while True:
            with self._lock:
                due = self._next
            if time.time() >= due:
                if self._in_quiet_hours():
                    wake  = self._next_wake_time()
                    delay = (wake - datetime.now()).total_seconds()
                    with self._lock:
                        self._next = time.time() + delay
                    logging.info(f"Quiet hours — holding until {wake.strftime('%H:%M')}")
                else:
                    self._fire()
            time.sleep(20)


# ─── Entry point ───────────────────────────────────────────────────────────────

def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def acquire_pid_lock():
    """Exit if another instance is already running."""
    if os.path.exists(PID_FILE):
        try:
            existing_pid = int(open(PID_FILE).read().strip())
            os.kill(existing_pid, 0)  # signal 0 = just check existence
            print(f"Already running as PID {existing_pid}. Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file — overwrite it
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))


def main():
    setup_logging()
    config = load_config()

    if "--setup" in sys.argv or "--topic" in sys.argv:
        print(f"\n{'─'*52}")
        print(f"  📱  NTFY TOPIC TO SUBSCRIBE TO:")
        print(f"      {config['topic']}")
        print(f"{'─'*52}")
        print(f"  Server  : ntfy.sh  (default in the app)")
        print(f"  Topic   : {config['topic']}")
        print(f"{'─'*52}\n")
        return

    if "--reset" in sys.argv:
        config["streak"] = 0
        config["total_breaks"] = 0
        save_config(config)
        print("Streak and total breaks reset to 0.")
        return

    if "--test" in sys.argv:
        logging.info("Sending test notification…")
        r = BreakReminder(config)
        r._fire()
        logging.info("Test sent! Check your ntfy app.")
        return

    acquire_pid_lock()
    daemon = BreakReminder(config)
    daemon.run()


if __name__ == "__main__":
    main()
