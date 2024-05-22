# Summary

This package provides a utility that translates [tests plans](https://learn.microsoft.com/en-us/azure/devops/test/overview?view=azure-devops), suites and cases in Azure DevOps (ADO) into validated gherkin feature files, and then uses `pytest-bdd generate` to create the runners for those tests.

After that, it can validate that the test directory has all of the necessary fixtures to run pytest using pytest-bdd given/when/then fixture decorators.

It leverages ADO's notion of [Shared Steps](https://learn.microsoft.com/en-us/azure/devops/test/share-steps-between-test-cases?view=azure-devops) to reduce duplication when authoring the features and scenarios.  This lets given/when/then clauses to be written once, and used many times.

It is capable of leveraging parameters on the test case (both "shared" and "non-shared") as `Examples`, creating a `Scenario Outline` instead of the standard `Scenario`.

# Installation
This package is written only in python, and can be installed using:
```bash
$ pip install adotestplan-to-pytestbdd
```

# Usage

```python
from adotestplan_to_pytestbdd import ADOTestPlan
url = 'https://dev.azure.com/[ORGANIZATON_HERE]'
pat = '[PAT_HERE]'
project = '[PROJECT_HERE]'
out_dir='output'
tp = ADOTestPlan(organization_url=url, pat=pat, project=project, out_dir=out_dir)
```

The above example hasn't yet "done anything", and is equivalent to the following:

```python
from adotestplan_to_pytestbdd import ADOTestPlan
tp = ADOTestPlan()
tp.url = 'https://dev.azure.com/[ORGANIZATON_HERE]'
tp.pat = '[PAT_HERE]'
tp.project = '[PROJECT_HERE]'
tp.out_dir='output'
```

Put differently - until one of the built-in methods is invoked,  properties can be set via init or via `property` access.

To populate the internal memory structures from ADO:
```python
tp.populate()
```

Next, to write feature files to disk from the populated:
```python
tp.write_feature_files()
```

At this point, the ADO test plan has been synchronized to feature files on disk.  Its possible that is a sufficient stopping point.

At this point begins the pytest-bdd integration.

First, use this method:
```python
tp.write_pytestbdd_runners()
```
to create test_xyz.py files on disk corresponding to the feature files generated above.  This is a wrapper around `pytest-bdd generate` (see [ado_test_plan.py](adotestplan_to_pytestbdd/ado_test_plan.py#:~:text=_generate_pytestbdd_for_feature)).

One reason this is seen as useful is that it avoids "checking in" boilerplate/generated code - the test methods created here are _basically_ stubs, the majority of the test occurs in the given/when/then fixtures.  With this approach, the test_xyz.py files can be just as ephemeral as the .feature files they are generated from - the one piece that is persistent/checked in is the fixtures where the actual test implementation occurs.

At this point, call:
```python
tp.validate_pytestbdd_runners_against_feature_files()
```

This final call uses pytest utilities to collect all fixtures in the specified test directory, and compares those against the needed fixtures, determined during the `populate()` phase.  It will print informative messages, and in the end raise an exception if some fixtures are not found.

# Testing
Please see [TESTING.md](TESTING.md) for notes on running the tests associated with this package.  Note this refers to the unit test for validating the package itself, not the tests generated by running this package normally.  That can be done after code generation via a normal call to `pytest`.

# Possible Enhancements

 - Split the 2 basic pieces of functionality into a separate package (1 being ADO to feature file translation, 2 being feature file to fixture "pool" checking)
 - Use `pytest --fixtures` to collect available fixtures instead of raw searching through files.  This is likely a more robust way of making sure fixtures aren't being missed. (this was looked into briefly, and it appears to be MUCH less performant than raw searching (on the order of 7s compared to 30ms), so this was tabled for now)
 - Document how "tags" can be used to filter test cases if one wanted to extend this utility for their own workflows.
 - A lot of the functionality in the [tasks.py](tasks.py) methods `delete_test_work_items` and `generate_test_work_items` may be revelant for "round tripping" this utility - going from .feature files _into_ ADO, which has some utility in and of itself - for instance, migrating from a plain-text file approach to an ADO Test Plan backed approach.