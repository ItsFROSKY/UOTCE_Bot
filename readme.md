# UOTCE Tele Bot

telegram bot + mini app for computer engineering students at the university of technology.
runs on vercel (fastapi webhook) and cronPing keeps it warm so theres almost no cold start.

Mini App programmed by **Taha AlAnbagi #1**

دليل الاستخدام الكامل بالعربي: [GUIDE.md](GUIDE.md)

## features

mini app (opens from the bot button or from the batch group):
- all the course files by stage and subject, with search that understands arabic spelling (المحاضره = المحاضرة)
- recently added files + favorites
- send any file to your chat with one tap (google docs and files over 50MB open in drive)
- follow any folder and the bot messages you the second a new file is added
- weekly schedule (today / tomorrow, sections A/B, shows which lecture is on right now)
- announcements banner (postponed lecture, room change, exams...) that also gets sent to everyone and the group
- deadlines with countdowns + reminders a day and 3 hours before
- absence tracker: tap a lecture in the schedule to log an absence, it calculates your % per subject from the schedule hours and warns you before you hit the limit (10% without excuse, 15% with excuse, like the ministry rules)
- exams with countdowns, reminders to everyone a week and a day before, and they show at the top of the pinned group message
- القعدة tab: **Subnet Rush** (10 subnetting questions, 20s each, speed points + combo, iraqi titles from توه بادي 🐣 to عمو الـ Subnet 👑), weekly and all time board, and the weekly champion gets posted in the group
- batch polls (انطلب تأجيل؟ اي يوم للتعويضي؟): anonymous, you see results after voting, results posted in the group when it closes
- follows telegram light/dark theme

bot commands: `/start` `/help` `/app` `/schedule` `/exams` `/top` `/myid` `/drive` `/tasks` `/source`

owner (ADMIN_IDS): `/add_admin` `/remove_admin`

admin: `/admins` `/announce` `/pin_app` `/poll` `/exam_add` `/exam_del` `/backup` `/set_group` `/unset_group` `/schedule_set` `/schedule_add` `/check_files` `/addtask` `/deltask` `/reindex` `/setup_app` `/tasks_update` `/update_tele` `/reset_topics` `/groupid`

## security fixes

- `/webhook` now checks the secret token, before anyone could send a fake update with an admin id and run admin commands
- callback buttons only accept files inside the course folder (callback data can be faked with a userbot and the service account sees the whole drive)
- `/reset_topics` is admin only now
- users dont see python errors anymore
- the mini app api checks telegram initData on every request + rate limits

## files

- `drive_index.py` crawls the drive once and caches it in redis (30 min), so browsing and search are instant
- `functions_bot.py` saves telegram's file_id after the first send so the next one is instant
- `campus.py` follows, announcements, schedule, exams, absences
- `fun.py` subnet rush, leaderboard, polls
- `webapp_api.py` the api for the mini app
- `tasks_source.py` deadlines from notion + `/addtask`
- `public/app/index.html` the mini app itself

## setup

env variables (the old ones stay the same):

- `WEBHOOK_SECRET` any random string (required)
- `CRON_SECRET` any random string, for the cron url
- `WEBAPP_URL` optional, default is `https://<vercel domain>/app/index.html`
- `ADMIN_IDS` the owners, comma separated telegram ids (send `/myid` to the bot to get yours). owners can add more admins from the bot with `/add_admin <id>` or by replying to someones message with `/add_admin`
- `NOTION_TOKEN` + `NOTION_DB_ID` optional, for deadlines from notion
- `ABSENCE_LIMIT` (10), `ABSENCE_LIMIT_EXCUSED` (15), `ABSENCE_NOTICE` (5), `SEMESTER_WEEKS` (15) optional, change them if your department uses different numbers

1. set the webhook with the secret:
   ```
   curl "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" -d url="https://<app>.vercel.app/webhook" -d secret_token="$WEBHOOK_SECRET"
   ```
2. BotFather > Bot Settings > Configure Mini App > put the `WEBAPP_URL` (needed for the group buttons)
3. send `/setup_app` to the bot once
4. make the bot admin in the batch group (with pin messages) and send `/pin_app` there. it pins a message with buttons that open the mini app + todays lectures + last announcement, and it updates itself. students can also send `/app` in the group anytime
5. add the schedule once:
   ```
   /schedule_set
   الأحد | 08:30-10:30 | Digital Logic | قاعة 3 | نظري
   الأحد | 10:30-12:30 | Electronics | مختبر 2 | عملي | A
   ```
   (day | time | subject | room | type | section, last 3 are optional). after that edit it from the app
6. cronPing every 15-30 min: `https://<app>.vercel.app/api/cron/tick?key=<CRON_SECRET>`
7. add exams from the app (الجدول > امتحان) or `/exam_add Digital Logic | شهري | 25/10 | 09:00 | قاعة 3`
8. send `/check_files` once so it remembers the files that already exist

students who open the app from the group need to press Start in the bot once so it can send them files.

## notes

- upstash free = 500k commands a month. normal browsing doesnt touch redis for rate limits anymore, so a batch of a few hundred students fits. if it gets close, set the cron to every 30 min
- send `/backup` to the bot in private every now and then, upstash free has no backups
- share only the course folders with the google service account, it can see anything shared with it

## local

```
pip install -r requirements.txt
uvicorn main:app --reload
```
open `http://127.0.0.1:8000/app` (outside telegram it shows demo data)

tests: `pip install -r requirements-dev.txt` then `pytest -q`
