# LinkedIn Job Alerts
Searches LinkedIn every day for new job postings that match a list of job titles, and sends the new ones in an email (or a notification on your phone).

![Example email](email.png)

## How it works
Each search in `config.toml` has a job title, a location, and a time frame like the past 24 hours. The script uses LinkedIn's public job search, the same one you get when you are logged out, so no account or cookies are needed. It goes through the results page by page and pulls out the title, company, location, posting time, salary, and company logo for each job.

LinkedIn's search also matches job descriptions, so a lot of what comes back is not actually the job you searched for. Because of this, each job title is checked against the search title, and a job is only kept if its title covers every word of the search. Similar words count as the same word, so a search for Software Engineer Co-op also finds Software Developer Intern, SWE Internship, and Backend Engineer Co-op. The same groups of similar words are used to build the LinkedIn query, joining the alternatives with OR so one request covers all of them. Jobs can also be blocked by words in their title with `block_titles`, for example to skip senior roles, and similar words count there too, so blocking "senior" also blocks "Sr." Companies can be blocked the same way with `block_companies`. Both can go under `[settings]` to apply to every search, or inside one search.

Searches can also be limited to one work term, like Winter 2027 (January to April). The term is read from the job title, or from the description when the title does not say, by looking for things like "Winter 2027", "Winter term", "January to April 2027", or a start date. Postings for a different term are dropped, and postings that never say when they are still get sent with a "Term not stated" label (or dropped, if `require_term` is on). Dates about school, like "graduating in Fall 2027", are ignored.

Every job that gets sent is saved in `seen_jobs.json`, so the same posting is never sent twice. LinkedIn also lists the same job once for every city it is in, so those are merged into one entry. The new jobs are grouped by search and sent as an HTML email, and optionally as a phone notification through ntfy. The computer's scheduler (launchd on macOS, cron on Linux, or Task Scheduler on Windows) runs the script at the times you pick, like once or twice a day. If nothing new turns up for 3 days (`checkin_days`), it sends a short check-in email instead, so a broken setup does not look the same as a quiet week. The check-in also says if runs have been failing.

## Results
On the Software Engineer Intern search in Canada, matching similar titles kept 4 of the first 20 LinkedIn results instead of 1. The extra matches were co-op and French language postings (like Stagiaire développeur(se) de logiciels, which is a software developer internship) that the exact word filter threw out. Titles like Mechanical Engineering Co-op, Software Analyst Intern, and Software Engineer, Internal Tools are still filtered out.

With the term set to Winter 2027, a week of co-op postings in Canada across four searches came out as 18 confirmed for Winter 2027, 7 that never said when, and 10 for other terms that were dropped. Most terms came from the title, but some were only in the description, like a City of Ottawa posting whose description said "Co-op Placement - Winter Term". The term check was also tested on 34 examples (10 real postings and 24 tricky phrasings, like a Summer internship that mentions graduating in Fall 2027), and got all of them right.

## Limitations
LinkedIn rate limits its public search after only a few quick requests. The script waits a few seconds between pages and backs off when it gets blocked. If a search still gets blocked part way through, the jobs from the pages it already loaded are kept, and a run stops after 30 minutes of the computer being awake (`max_run_minutes`) and sends what it found. Searches that did not finish are picked up by the next run, since its time frame overlaps, so they do not cause an email on their own. They are mentioned when an email goes out anyway, and the check-in email says if runs have been failing.

The scraping depends on the HTML of LinkedIn's search results. If LinkedIn changes it, the script can no longer read the listings, and it sends a warning email saying so instead of quietly finding nothing. The selectors in `parse_jobs()` (and `fetch_description()` for the term check) then need updating.

The groups of similar words are written by hand and lean towards software jobs. Other kinds of titles only match on their exact words unless more groups are added in `config.toml`.

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

## How to Run
```bash
git clone https://github.com/KairavT/linkedin-job-alerts.git
cd linkedin-job-alerts
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.toml config.toml
python3 job_alerts.py --preview
```
On Windows, use `python` instead of `python3`, `venv\Scripts\activate` instead of `source venv/bin/activate`, and `copy` instead of `cp`.

Fill in `.env` with a Gmail address and an [app password](https://myaccount.google.com/apppasswords) (a normal Gmail password will not work), or an ntfy topic, and put the jobs to search for in `config.toml`. `--preview` opens the email in the browser without sending anything, `--dry-run` prints the results instead, and `--test-notify` sends a test alert. `--lookback` does a one-off catch-up over the past 48 hours, for example after the computer was off for a day or two. A different time frame can be given, like `--lookback 3d`, but the point is to spot jobs early, so it is kept short by default.

## Running it on a schedule
The script only runs when something starts it, so it needs your computer's scheduler to start it at the times you want. These examples run it at 8am and 8pm.

### macOS
```bash
./schedule.sh 8:00 20:00
```
This sets up a launchd job. Any number of times works, like `./schedule.sh 8:00` for once a day. `./schedule.sh --remove` stops it, and the output of each run goes to `logs/job_alerts.log`.

The Mac has to be on and awake at the scheduled times for the email to arrive on time. If it is asleep (for example with the lid closed), macOS may still start the run during one of the short background wakes it does while sleeping, but those only last a few seconds, so the run finishes once the Mac is opened again and the email arrives then. If several runs were missed while it slept they are combined into one. If the Mac is shut down at a scheduled time, that run is skipped.

### Linux
Run `crontab -e` and add this line, with the path to your copy of the project:
```
0 8,20 * * * /path/to/linkedin-job-alerts/venv/bin/python /path/to/linkedin-job-alerts/job_alerts.py --log logs/job_alerts.log
```
`--log` makes the script write its own log file, in the project's `logs` folder. cron skips any run that comes up while the computer is asleep or off.

### Windows
In PowerShell, from the project folder:
```powershell
$dir = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$dir\venv\Scripts\pythonw.exe" -Argument "job_alerts.py --log logs\job_alerts.log" -WorkingDirectory $dir
$times = (New-ScheduledTaskTrigger -Daily -At 8am), (New-ScheduledTaskTrigger -Daily -At 8pm)
Register-ScheduledTask -TaskName "LinkedIn job alerts" -Action $action -Trigger $times -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```
`pythonw.exe` runs the script without opening a window, and `--log` makes it write its own log file, since Task Scheduler does not save output. If the computer was asleep or off at a scheduled time, the run starts as soon as it is back on. `Unregister-ScheduledTask -TaskName "LinkedIn job alerts"` stops it.
