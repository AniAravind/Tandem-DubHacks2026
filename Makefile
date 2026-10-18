# Handy shortcuts. Run them from the project folder with the virtual environment turned on, for example:  make test
PY ?= python

.PHONY: run test seed demo-reset demo-check

run:           ## start the website on http://localhost:8000
	$(PY) manage.py runserver

test:          ## run every automated test
	$(PY) manage.py test

seed:          ## load the demo data (safe to repeat)
	$(PY) manage.py seed_demo

demo-reset:    ## wipe the demo group's activity and load the original seed again, then check it
	$(PY) manage.py demo_reset
	$(PY) manage.py demo_check

demo-check:    ## confirm the ids, listings and prices still match the seed
	$(PY) manage.py demo_check
