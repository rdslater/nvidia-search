## Crude Job Search
Trying to keep on top of job postings is difficult.  SOmetimes the search functions are not what we like.  So I decided to create my own search

I did use AI to automate this task, but I still know what is going on.  I ping nvidias workday and download the job descriptions to a CSV.  This allows me to immediately filter for recent postings.  There is a crude scoring mechaninsm but I grab em all to review.  There are some filters, right now it does grab everything and the filter for the US is incorrect.  I know how to fix it, so if you want to duplicate this you may have to understand python and figure out the variable to edit.
It does not apply for jobs.  I still do that the old fashioned way tailoring my resume to the JD (AND NOT MAKING UP FAKE CREDENTIALS!!)

Here's the text of the file which claude so generously built for me:

NVIDIA job tracker
==================
Pulls job postings from NVIDIA's Workday careers site (via the same JSON API the
website itself uses), stores them in a local SQLite database, and tells you which
ones are NEW since the last run.

Usage
-----
    pip install requests

    python nvidia_jobs.py                      # scrape, store, export CSVs, show new jobs
    python nvidia_jobs.py --search "firmware"  # only scrape postings matching a keyword
    python nvidia_jobs.py --list-new           # show jobs you haven't triaged yet
    python nvidia_jobs.py --find "cuda AND compiler"   # full-text search the database
    python nvidia_jobs.py --mark-applied JR1234567 JR7654321
    python nvidia_jobs.py --mark-ignored JR1111111
    python nvidia_jobs.py --export             # just rewrite the CSVs from the DB

Files (created next to this script, or in --data-dir):
    nvidia_jobs.db            SQLite database (the source of truth)
    nvidia_jobs.csv           tracking sheet: every job ever seen (open in Excel)
    new_jobs_YYYY-MM-DD.csv   only the jobs first seen on that run date

Run it daily with Task Scheduler (Windows) or cron (WSL) -- see README notes at
the bottom of this file.
