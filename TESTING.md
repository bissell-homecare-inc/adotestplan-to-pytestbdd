# Setup

In order to execute the unit tests This package, it is expected that you have the test scenarios created.  We chose to do this, rather than always creating and deleting them in test setup and teardown to avoid unnecessary accumulation/incrementing of ADO work item IDs.

To expedite creation of those "test support" work items, the `tasks.py` file in this repository has a couple helper functions. This implies a dependency on `invoke` (which is listed as a dev dependency).  Run `invoke generate-test-work-items > .new_env` once to setup the necessary work items.  It will print out some test plan IDs that get piped to your .env file.  If you want to see them, replace the `>` with `| tee` . When that is done, the .new_env file should be formatted like so, with the data populated:

```bash
TEST_PAT=
TEST_URL=
TEST_PROJ=
TEST_EMPTY_TP=
TEST_NON_EMPTY_TP=
TEST_SHARED_STEPS_AND_SHARED_PARAMETERS_TP=
TEST_NON_SHARED_PARAMETERS_TP=
TEST_INVALID_GHERKIN_TP=
TEST_EMPTY_PARAMETERS_TP=
```

Now rename that file to `.env`:

```bash
mv .new_env .env
```

After that, you can run `poetry run pytest` or `invoke tests` and it should pass.

Note that you only need to do that `invoke generate-test-work-items` once - as long as you leave those work items unmodified, they should be re-usable going forward.

There is also an `invoke delete-test-work-items` but use that one with EXTREME caution - that deletes ALL test plans, suites, cases, shared steps, and shared parameters within your project.

## Code Coverage

If you run `invoke tests`, pytest is actually run through `coverage`([docs](https://coverage.readthedocs.io/en/7.4.4/)).  You can run it outside invoke by running `poetry run coverage run -m pytest`.  After this completes, you should have a .coverage file in the root directory.  You can then run either `poetry run coverage xml --skip-empty` or `poetry run coverage html --skip-empty` to generate a human readable document to view the coverage results.

## Python Version Compatibility
You can run `tox` (after installing it via `pip install tox`).  This will run the same pytests executed above in all enviroments listed in the `tool.tox` section of the `pyproject.toml` file.