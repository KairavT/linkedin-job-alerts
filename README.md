# LinkedIn Job Alerts
Searches LinkedIn every day for new job postings that match a list of job titles, and sends the new ones in an email (or a notification on your phone).

![Example email](email.png)

## How it works
Each search in `config.toml` has a job title, a location, and a time frame like the past 24 hours. The script uses LinkedIn's public job search, the same one you get when you are logged out, so no account or cookies are needed. It goes through the results page by page and pulls out the title, company, location, posting time, salary, and company logo for each job.

LinkedIn's search also matches job descriptions, so a lot of what comes back is not actually the job you searched for. Because of this, each job title is checked against the search title, and a job is only kept if its title covers every word of the search. Similar words count as the same word, so a search for Software Engineer Co-op also finds Software Developer Intern, SWE Internship, and Backend Engineer Co-op. The same groups of similar words are used to build the LinkedIn query, joining the alternatives with OR so one request covers all of them.

Every job that gets sent is saved in `seen_jobs.json`, so the same posting is never sent twice. The new jobs are grouped by search and sent as an HTML email, and optionally as a phone notification through ntfy. A launchd job runs the script once a day.

## Results
On the Software Engineer Intern search in Canada, matching similar titles kept 4 of the first 20 LinkedIn results instead of 1. The extra matches were co-op and French language postings (like Stagiaire développeur(se) de logiciels, which is a software developer internship) that the exact word filter threw out. Titles like Mechanical Engineering Co-op, Software Analyst Intern, and Software Engineer, Internal Tools are still filtered out.

## Limitations
LinkedIn rate limits its public search after only a few quick requests. The script waits a few seconds between pages and backs off when it gets blocked, and any search that still fails is mentioned in the email and tried again the next day.

The scraping depends on the HTML of LinkedIn's search results. If LinkedIn changes it, every search will come back empty until the selectors in `parse_jobs()` are updated.

The groups of similar words are written by hand and lean towards software jobs. Other kinds of titles only match on their exact words unless more groups are added in `config.toml`.

launchd only runs the script while the Mac is on. If the Mac is asleep at the scheduled time it runs when it wakes up, but if it is shut down that day is skipped, so a time frame longer than 24 hours is safer.

Scraping LinkedIn goes against its User Agreement. This only reads public job listings, and only around 20 requests a day.

## Files
- `job_alerts.py` searches LinkedIn, filters the results, and sends the email and notification
- `config.toml` the job titles, locations, time frame, and filters to search for
- `schedule.sh` sets up (or removes) the daily launchd job
- `.env.example` template for the email and ntfy settings

## Tech Stack
- Python
- Requests
- Beautiful Soup
- launchd
- ntfy

## How to Run
```bash
git clone https://github.com/KairavT/linkedin-job-alerts.git
cd linkedin-job-alerts
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 job_alerts.py --preview
```
Fill in `.env` with a Gmail address and an [app password](https://myaccount.google.com/apppasswords) (a normal Gmail password will not work), or an ntfy topic, and put the jobs to search for in `config.toml`. `--preview` opens the email in the browser without sending anything, `--dry-run` prints the results instead, and `--test-notify` sends a test alert.

To run it every day at 8am:
```bash
./schedule.sh 8:00
```
`./schedule.sh --remove` stops it, and the output of each run goes to `logs/job_alerts.log`.
