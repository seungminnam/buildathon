"""pytest plugin: write the node ids of tests that fail, error or time out.

A test with subtests counts as failed when any subtest fails. A file that
cannot be collected is written as "<collect>:<path>". Output: $LABEL_OUT.
"""

import json
import os

_failed = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid.split(" ")[0])


def pytest_collectreport(report):
    if report.failed:
        _failed.add("<collect>:" + report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["LABEL_OUT"], "w") as out:
        json.dump({"failed": sorted(_failed), "exit": int(exitstatus)}, out)
