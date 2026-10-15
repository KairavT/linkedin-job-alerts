# LinkedIn Job Alerts
Searches LinkedIn every day for new job postings that match a list of job titles, and sends the new ones in an email (or a notification on your phone).

![Example email](email.png)

## How it works
Each search in `config.toml` has a job title, a location, and a time frame like the past 24 hours. The script uses LinkedIn's public job search, the same one you get when you are logged out, so no account or cookies are needed. It goes through the results page by page and pulls out the title, company, location, posting time, salary, and company logo for each job.

LinkedIn's search also matches job descriptions, so a lot of what comes back is not actually the job you searched for. Because of this, each job title is checked against the search title, and a job is only kept if its title covers every word of the search. Similar words count as the same word, so a search for Software Engineer Co-op also finds Software Developer Intern, SWE Internship, and Backend Engineer Co-op. The same groups of similar words are used to build the LinkedIn query, joining the alternatives with OR so one request covers all of them. Jobs can also be blocked by words in their title with `block_titles`, for example to skip senior roles, and similar words count there too, so blocking "senior" also blocks "Sr." Companies can be blocked the same way with `block_companies`. Both can go under `[settings]` to apply to every search, or inside one search.

Searches can also be limited to one work term, like Winter 2027 (January to April). The term is read from the job title, or from the description when the title does not say, by looking for things like "Winter 2027", "Winter term", "January to April 2027", or a start date. Postings for a different term are dropped, and postings that never say when they are still get sent with a "Term not stated" label (or dropped, if `require_term` is on). Dates about school, like "graduating in Fall 2027", are ignored.

Every job that gets sent is saved in `seen_jobs.json`, so the same posting is never sent twice. LinkedIn also lists the same job once for every city it is in, so those are merged into one entry. Reposted jobs are another catch: when a company reposts an old job, LinkedIn shows it as just posted, even if it already has hundreds of applicants. LinkedIn's public pages don't say a job was reposted, but job IDs go up over time and a repost keeps its original ID, so a "new" job whose ID is more than 2 days older than the time frame (3 days, for the past 24 hours) is labelled as a likely repost. With `include_reposts = false`, a likely repost is skipped once its page shows 100 or more applicants (`repost_applicants`). Ones with fewer are kept and labelled, because some jobs are drafted days before they go up, and those are as new as any other posting. The new jobs are grouped by search and sent as an HTML email, and optionally as a phone notification through ntfy. The computer's scheduler (launchd on macOS, cron on Linux, or Task Scheduler on Windows) runs the script at the times you pick, like once or twice a day. If nothing new turns up for 3 days (`checkin_days`), it sends a short check-in email instead, so a broken setup does not look the same as a quiet week. The check-in also says if runs have been failing.

## Results
On the Software Engineer Intern search in Canada, matching similar titles kept 4 of the first 20 LinkedIn results instead of 1. The extra matches were co-op and French language postings (like Stagiaire développeur(se) de logiciels, which is a software developer internship) that the exact word filter threw out. Titles like Mechanical Engineering Co-op, Software Analyst Intern, and Software Engineer, Internal Tools are still filtered out.

With the term set to Winter 2027, a week of co-op postings in Canada across four searches came out as 18 confirmed for Winter 2027, 7 that never said when, and 10 for other terms that were dropped. Most terms came from the title, but some were only in the description, like a City of Ottawa posting whose description said "Co-op Placement - Winter Term". The term check was also tested on 34 examples (10 real postings and 24 tricky phrasings, like a Summer internship that mentions graduating in Fall 2027), and got all of them right.

## Limitations
LinkedIn rate limits its public search after only a few quick requests. The script waits a few seconds between pages and backs off when it gets blocked. If a search still gets blocked part way through, the jobs from the pages it already loaded are kept, and a run stops after 30 minutes of the computer being awake (`max_run_minutes`) and sends what it found. Searches that did not finish are picked up by the next run, since its time frame overlaps, so they do not cause an email on their own. They are mentioned when an email goes out anyway, and the check-in email says if runs have been failing.

The scraping depends on the HTML of LinkedIn's search results. If LinkedIn changes it, the script can no longer read the listings, and it sends a warning email saying so instead of quietly finding nothing. The selectors in `parse_jobs()` (and `fetch_description()` for the term check) then need updating.

The groups of similar words are written by hand and lean towards software jobs. Other kinds of titles only match on their exact words unless more groups are added in `config.toml`.

Repost detection is an estimate based on job IDs, which go up by roughly half a million a day. A job reposted within a couple of days of first going up is not caught. Checking the applicant count on likely reposts means opening each one's page, which adds a request per likely repost.

The term check is based on patterns in the text, so a posting that describes its dates in an unusual way can be dropped or kept by mistake. Spring is treated as both Winter and Summer, since it means January to May at most US schools and May to August at most Canadian ones.

The script runs on your own computer, not on a server, so alerts only go out while the computer is on (see Running it on a schedule). A run that is missed while the computer is off is skipped, so the time frame should be longer than the gap between runs, or postings from that gap will be missed. The 30 minute limit only counts time the computer is awake on macOS and Linux. On Windows, time spent asleep may count toward it too.

Scraping LinkedIn goes against its User Agreement. This only reads public job listings, with a few dozen requests each run.

## Files
- `job_alerts.py` searches LinkedIn, filters the results, and sends the email and notification
- `config.example.toml` example config, copied to `config.toml` with the job titles, locations, time frame, and filters to search for
- `schedule.sh` sets up (or removes) the scheduled runs on macOS
- `.env.example` template for the email and ntfy settings

## Tech Stack
- Python
- Requests
- Beautiful Soup
- launchd, cron, or Task Scheduler
- ntfy

## Setting it up
You don't need to know how to code to set this up. It takes about 15 minutes and you only do it once. A few steps use the Terminal (on a Mac) or PowerShell (on Windows), which is a window where you type a command and press Enter. Each command below is meant to be copied in exactly as written.

### 1. Install Python
The script is written in Python, so your computer needs Python 3.11 or newer. Download it from [python.org/downloads](https://www.python.org/downloads/) and run the installer. On Windows, tick the box that says "Add python.exe to PATH" on the first screen of the installer, or the commands below won't find it.

### 2. Download the project
At the top of this page, click the green Code button, then Download ZIP. Unzip it and move the folder somewhere you'll keep it, like your Documents folder. The folder may be called `linkedin-job-alerts-main`, which is fine. (If you use git, `git clone https://github.com/KairavT/linkedin-job-alerts.git` does the same thing.)

### 3. Open a terminal in the project folder
On a Mac, open Terminal (search for it with Spotlight), type `cd` followed by a space, drag the project folder into the Terminal window, and press Enter.

On Windows, open the project folder in File Explorer, click the address bar at the top, type `powershell`, and press Enter.

Keep this window open, since every command in the next steps goes into it.

### 4. Install what the script needs
This creates a folder called `venv` inside the project that holds the two small libraries the script uses, so nothing else on your computer is changed.

On a Mac or Linux:
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```
On Windows:
```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### 5. Get a Gmail app password
The alerts are sent from your own Gmail account. Gmail does not let scripts use your normal password, so you make an app password, which is a separate 16 letter password just for this. First make sure 2-Step Verification is turned on for your Google account. Then go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), create an app password called "job alerts", and copy the 16 letters it shows you.

### 6. Fill in your email settings
This makes your own copy of the email settings file and opens it.

On a Mac:
```bash
cp .env.example .env
open -e .env
```
On Windows:
```powershell
copy .env.example .env
notepad .env
```
Put your Gmail address right after `SMTP_USER=`, the 16 letter app password right after `SMTP_PASSWORD=`, and the address you want the alerts sent to right after `EMAIL_TO=` (this can be the same Gmail address). Then save and close the file. This file stays on your computer and is never uploaded anywhere.

### 7. Choose the jobs to search for
This makes your own copy of the search settings and opens it.

On a Mac:
```bash
cp config.example.toml config.toml
open -e config.toml
```
On Windows:
```powershell
copy config.example.toml config.toml
notepad config.toml
```
Each `[[search]]` block near the bottom is one search. Change the `title` and `location` to the job you want, and copy a block to add another search or delete one to remove it. For example:
```toml
[[search]]
title = "Software Engineer Co-op"
location = "Canada"
```
The settings at the top apply to every search, and each one has a note next to it saying what it does. The ones you are most likely to change are `posted_within` (how far back to look), `term` (only co-ops for one work term, like "Winter 2027"), `include_reposts` (whether to keep old jobs reposted as new), and `block_titles` and `block_companies` (titles and companies to skip). A line that starts with `#` is switched off, so delete the `#` and the space after it to switch one on.

Text in this file has to be inside straight quotes ("), not curly ones (“), or the file won't load. TextEdit on a Mac changes quotes to curly ones as you type, so turn that off first under Edit, then Substitutions, then Smart Quotes.

### 8. Try it out
On a Mac or Linux:
```bash
venv/bin/python job_alerts.py --preview
```
On Windows:
```powershell
venv\Scripts\python job_alerts.py --preview
```
This searches LinkedIn and opens the email it would send in your browser, without sending anything. It can take a few minutes, because LinkedIn limits how quickly it can be searched. If the jobs look right, send yourself a test email by running the same command with `--test-notify` instead of `--preview`. If the test email doesn't arrive, double check the address and app password in `.env`.

### 9. Make it run every day
The script only runs when something starts it, so the last step is telling your computer's scheduler to start it at the times you want. These examples run it at 8am and 8pm. The computer has to be on at those times for the email to arrive on time.

On a Mac:
```bash
bash schedule.sh 8:00 20:00
```
Any number of times works, like `bash schedule.sh 8:00` for once a day, and `bash schedule.sh --remove` stops it. The output of each run goes to `logs/job_alerts.log` in the project folder.

If the Mac is asleep at a scheduled time (for example with the lid closed), macOS may still start the run during one of the short background wakes it does while sleeping, but those only last a few seconds, so the run finishes once the Mac is opened again and the email arrives then. If several runs were missed while it slept they are combined into one. If the Mac is shut down at a scheduled time, that run is skipped.

On Windows, paste these four lines into PowerShell (still in the project folder):
```powershell
$dir = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$dir\venv\Scripts\pythonw.exe" -Argument "job_alerts.py --log logs\job_alerts.log" -WorkingDirectory $dir
$times = (New-ScheduledTaskTrigger -Daily -At 8am), (New-ScheduledTaskTrigger -Daily -At 8pm)
Register-ScheduledTask -TaskName "LinkedIn job alerts" -Action $action -Trigger $times -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```
This runs it in the background without opening a window, and keeps a log in `logs\job_alerts.log`. If the computer was asleep or off at a scheduled time, the run starts as soon as it is back on. To stop it, run `Unregister-ScheduledTask -TaskName "LinkedIn job alerts"`.

On Linux, run `crontab -e` and add this line, replacing both `/path/to/linkedin-job-alerts` with where the project folder is (the `pwd` command shows it):
```
0 8,20 * * * /path/to/linkedin-job-alerts/venv/bin/python /path/to/linkedin-job-alerts/job_alerts.py --log logs/job_alerts.log
```
cron skips any run that comes up while the computer is asleep or off.

### Other options
To also get a notification on your phone, install the free ntfy app, subscribe to a topic name that only you would guess, and put that name right after `NTFY_TOPIC=` in `.env`. `--dry-run` shows the results in the terminal instead of the browser. `--lookback` does a one-off catch-up over the past 48 hours, for example after the computer was off for a day or two, and a different time frame can be given, like `--lookback 3d`, but it is kept short by default since the point is to spot jobs early.
