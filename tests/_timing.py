"""The slack a timing check allows over its target, by state backend. Shared by the page-times test
(ee/tests/test_factory_page_times.py, TR-1607) (the SRE home's test went with the AI SRE edition, TR-1809);
it lives here so a core test can use it in the run without ee.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _timing import SLACK
"""
# How much a measured time may exceed what it is held to. Postgres gets more: a round trip to the
# database (a container here, a service on CI) varies from run to run. The page-times test gives it to
# the routes in its OVER list only.
SLACK = {"sqlite": 1.25, "postgres": 1.5}
