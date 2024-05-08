import json
import logging
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import StringIO
from itertools import product
from pathlib import Path
from typing import List, TypedDict

import pydot
from azure.devops.connection import Connection
from azure.devops.v7_0.test.models import SuiteTestCase
from azure.devops.v7_0.test.test_client import TestClient
from azure.devops.v7_0.test_plan.models import TestPlan, TestSuite
from azure.devops.v7_0.test_plan.test_plan_client import TestPlanClient
from azure.devops.v7_0.work_item_tracking import WorkItemTrackingClient
from azure.devops.v7_0.work_item_tracking.models import WorkItem
from azure.devops.v7_0.work_item_tracking_process import WorkItemTrackingProcessClient
from bs4 import BeautifulSoup
from gherlint.linter import GherkinLinter
from msrest.authentication import BasicAuthentication
from thefuzz import fuzz
from timebudget import timebudget

from adotestplan_to_pytestbdd.exceptions import (
    InvalidGherkinError,
    InvalidParameterError,
    InvalidStepError,
    MissingFixturesError,
    NoTestSuiteError,
    OrderOfOperationsError,
)


@dataclass
class Step:
    id: int = 0
    text: str = ""
    revision: int = 0

    def __str__(self):
        return self.text


@dataclass
class Background:
    name: str = ""
    revision: int = 0
    steps: List[Step] = field(default_factory=list)


class Parameter(TypedDict):
    name: str = ""  # this is the key
    value: List[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: int = 0
    revision: int = 0
    name: str = ""
    steps: List[Step] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    is_outline: bool = False
    is_background: bool = False
    ado_work_item: WorkItem = None
    non_shared_parameters: Parameter = field(default_factory=dict)

    def __str__(self):
        return self.name


@dataclass
class Feature:
    id: int = 0
    name: str = ""
    revision: int = 0
    scenarios: List[Scenario] = field(default_factory=list)
    background: Background = None

    def __str__(self):
        return self.name


@dataclass
class SharedParameters:  # don't use typed dict here because we only want 1 possible key
    id: int = 0  # this is the key
    revision: int = 0
    parameters: Parameter = field(default_factory=dict)


@dataclass
class BDDTestPlan:
    features: List[Feature] = field(default_factory=list)
    shared_parameters: SharedParameters = field(default_factory=dict)


class AzureDevOpsTestPlan:
    """Test Plan Class to import test suites from
    ADO and parse to BDD feature files"""

    def __init__(
        self,
        firmware_part_number=None,
        pat=None,
        organization_url: str = None,
        profile=True,
        name: str = None,
        id: str = None,
        project: str = None,
        fixtures: str = "fixtures",
        out_dir: str = "gen",
        ignore_tags_list: list = None,
    ):
        timebudget.set_quiet()
        self.profile = profile
        self.firmware_part_number = firmware_part_number
        self.pat = pat
        self.organization_url = organization_url

        # these parameters are "properties" can be filled out
        # later if they weren't provided at init time
        self._name = name
        self._id = id
        self._project = project
        self._fixtures = fixtures
        self._out_dir = out_dir
        self._valid_states = []
        if ignore_tags_list is None:
            self._ignore_tags_list = []
        else:
            self._ignore_tags_list = ignore_tags_list
        # pre-populate some fields
        self.bdd_tp = BDDTestPlan()
        self._azure_test_suites = []
        self._shared_steps = {}

        self.valid_starters = ["given", "when", "then", "but", "and"]

    def populate(self):
        self._open_ado_connection()
        self._get_ado_clients()
        self._get_azure_test_case_valid_states()
        self.get_azure_test_plan()
        self._get_azure_test_suites()
        # at this point, we have read in most of the azure items.
        # now, we start converting them to BDD
        self._populate_bdd_features_from_azure_test_suites()

        # there is a bit more fetching from azure at this point
        # since we now know which shared steps are used
        self._get_azure_shared_steps()

        self._link_shared_steps_back_to_bdd_scenarios()

        # the final bit of fetching will get all shared parameter values
        self._get_azure_shared_params()
        if self._profile:
            timebudget.report(reset=True)
        # At the end of the populate function, no files have been written
        # but the self.bdd_tp dictionary should be complete and READY
        # to write to disk.  We don't do that here though in case
        # further validation is desired.

    @property
    def plan_id(self):
        return self._id

    @plan_id.setter
    def plan_id(self, value):
        self._id = value

    @property
    def pat(self):
        if self._pat is None:
            self._pat = os.getenv("PAT")
        return self._pat

    @pat.setter
    def pat(self, value):
        self._pat = value

    @property
    def profile(self):
        return self._profile

    @profile.setter
    def profile(self, value):
        self._profile = value

    @property
    def project(self):
        return self._project

    @project.setter
    def project(self, value):
        self._project = value

    @property
    def valid_states(self):
        return self._valid_states

    @valid_states.setter
    def valid_states(self, value):
        self._valid_states = value

    @property
    def fixtures(self):
        return self._fixtures

    @fixtures.setter
    def fixtures(self, value):
        self._fixtures = value

    @property
    def out_dir(self):
        return self._out_dir

    @out_dir.setter
    def out_dir(self, value):
        self._out_dir = value

    @property
    def ignore_tags_list(self):
        return self._ignore_tags_list

    @ignore_tags_list.setter
    def ignore_tags_list(self, value):
        self._ignore_tags_list = value

    @timebudget
    def _build_examples_outline(self, nonshared_parameters, examples_to_match):
        """This subroutine will loop through all of the ADO shared parameters in a given
        test scenario, and generate a gherkin formatted examples table from them to
        be placed in the features file."""
        examples_str = ""
        examples_version_str = ""  # unused for non-shared parameters
        # first, merge shared and non-shared
        all_parameters = {}
        for shared_parameter_id in self.bdd_tp.shared_parameters:
            shared_parameter = self.bdd_tp.shared_parameters[shared_parameter_id]
            examples_version_str += f"\t\t\t# Shared Parameters {shared_parameter_id}: Revision {shared_parameter.revision}\n"  # noqa: E501
            # these operations require python>=3.9
            all_parameters |= shared_parameter.parameters
        all_parameters |= nonshared_parameters  # these operations require python>=3.9
        if all(example in all_parameters for example in examples_to_match):
            # Corrected header formatting
            examples_str += (
                "\t\t\t| "
                + " | ".join(example.center(30) for example in examples_to_match)
                + " |\n"
            )
            # Iterate through each parameter set separately
            parameter_sets = []
            for example in examples_to_match:
                temp = []
                parameter = all_parameters[example]
                if isinstance(parameter, list):
                    for param_val in parameter:
                        temp.append(param_val)
                else:
                    temp.append(parameter)
                parameter_sets.append(temp)
            combinations = product(*parameter_sets)

            for combo in combinations:
                examples_str += (
                    "\t\t\t| "
                    + " | ".join([str(value).center(30) for value in combo])
                    + " |\n"
                )
        else:
            missing_fields = [
                example
                for example in examples_to_match
                if example not in all_parameters
            ]
            if missing_fields:
                logging.warning(f"Outline Params not found:{', '.join(missing_fields)}")

        examples_str += "\n"

        return examples_str, examples_version_str

    @timebudget
    def _write_scenario(self, scenario: Scenario, feature: Feature, file):
        logging.info(
            f"Writing scenario: {scenario.name}_{scenario.id} Revision: {scenario.revision}"
        )  # noqa: E501
        file.write(f"\t@{scenario.id} ")
        for tag in scenario.tags:
            if tag not in self._ignore_tags_list:
                file.write(f"@{tag} ")

        file.write("\n")
        if scenario.is_outline:
            file.write(
                f"\tScenario Outline: {scenario.name}_{scenario.id}_Revision_{scenario.revision}\n"
            )  # noqa: E501
        else:
            file.write(
                f"\tScenario: {scenario.name}_{scenario.id}_Revision_{scenario.revision}\n"
            )  # noqa: E501

        examples_to_match = []
        for step in scenario.steps:
            if scenario.is_outline:
                # note that here it still doesn't have the azuredevops "@" prefix
                # because that should have been removed by now.
                [
                    examples_to_match.append(x)
                    for x in re.findall(r"<(.*?)>", step.text)
                    if x not in examples_to_match
                ]
            file.write(f"\t\t{step.text}\n")
        file.write("\n")

        if scenario.is_outline:
            examples_str, examples_version_str = self._build_examples_outline(
                scenario.non_shared_parameters, examples_to_match
            )
            file.write("\t\tExamples:\n")
            file.write(examples_str)
            file.write(examples_version_str)
        return True

    @timebudget
    def _write_feature_file(self, feature: Feature):
        if not len(feature.scenarios):
            logging.warning(
                f"Feature {feature} has no scenarios in plan {self.plan_id}"
            )
            return
        filename = f"{self.out_dir}/{feature.name}.feature"

        logging.info(f"writing {feature.name} to {filename}")
        with open(filename, "w") as feature_file:
            feature_file.write(f"@{self.plan_id} @{feature.id}\n")
            feature_file.write(
                f"Feature: {feature.name}_{feature.id}_Revision_{feature.revision}\n\n"
            )

            if feature.background is not None:
                feature_file.write(
                    f"\tBackground: {feature.background.name}_{feature.background.id}_Revision_{feature.background.revision}\n"
                )  # noqa: E501
                for step in feature.background.steps:
                    feature_file.write(f"\t\t{step.text}\n")
                feature_file.write("\n")

            scenarios_written = 0
            for scenario in feature.scenarios:
                if self._write_scenario(scenario, feature, feature_file):
                    scenarios_written += 1

            if not scenarios_written:
                logging.warning(
                    f"No scenarios created for {feature} in {self.plan_id}. Check tags!"
                )

    @timebudget
    def write_feature_files(self):
        """this test plan populates a directory with BDD formatted feature files.
        it gets those feature files from an internal object self.bdd_tp, so that
        is assumed to have been populated previously"""
        if not self.bdd_tp.features:
            raise OrderOfOperationsError("BDD Test Plan has not been initialized")
        if os.path.exists(self.out_dir):
            # if it exists delete it and all files in it
            shutil.rmtree(self.out_dir)

        os.makedirs(self.out_dir)

        logging.info("BEGIN FEATURE FILE WRITE")

        for feature in self.bdd_tp.features:
            self._write_feature_file(feature)

    def _replace_placeholder(self, needed_fixture, defined_fixture):
        """this subroutine replaces the parse placeholders in G/W/T
        clauses with their variable names"""

        # pattern = re.escape(defined_fixture).replace(r'\{\}', r'(\S+)')
        pattern = re.escape(defined_fixture).replace(r"\{\}", r"([^\s]+)")
        match = re.match(pattern, needed_fixture)

        if match:
            updated_defined_fixture = defined_fixture
            for i in range(1, defined_fixture.count("{}") + 1):
                updated_defined_fixture = re.sub(
                    r"\{\}", match.group(i), updated_defined_fixture, count=1
                )
            return updated_defined_fixture
        else:
            return None

    @timebudget
    def _parse_scenario_runners_from_pytestbdd(self, generated_test_file):
        """This subroutine scraps all pytest generated python files and looks for
        the basic "runners", not given/when/thens, just the tests
        themselves."""
        temp_list = generated_test_file.split("\n\n")
        scenarios_runners = []
        for i in temp_list:
            if "from pytest_bdd" in i:
                scenarios_runners.append("from pytest_bdd import scenario")
            else:
                if "@given" not in i and "@when" not in i and "@then" not in i:
                    i = i.replace(f"{self.out_dir}/", "")
                    scenarios_runners.append(i)

        return scenarios_runners

    @timebudget
    def _collect_needed_fixtures(self, generated_test_file):
        """This subroutine will look at pytest-bdd generated output
        and see what fixtures would be EXPECTED to exist.
        This is later used for comparison against what ACTUALLY exists.
        """
        temp_list = generated_test_file.split("\n\n")
        fixtures_list = []
        for i in temp_list:
            temp_split = i.split("\n")
            for j in temp_split:
                if len(j) > 0:
                    if j[0] == "@" and "scenario" not in j:
                        fixtures_list.append(j)

        return fixtures_list

    def _touch_up_fixture_for_comparison(self, line):
        keyword = "parse"
        if keyword in line:
            for word in line.split("("):
                if keyword in word:
                    # due to line length optimization, pytestbdd's "parser"
                    # may have been renamed on import (import parsers as xyz)
                    # therefore, we need to discover the format of that string
                    replacement_term = word
                    break

            line = line.replace(replacement_term, "")
            line = line.replace("\n", "")
            while "((" in line or "))" in line:
                line = line.replace("((", "(")
                line = line.replace("))", ")")
        line = re.sub(r"\{.*?\}", "{}", line)
        return line.strip()

    @timebudget
    def _collect_defined_fixtures(self):
        """This subroutine scrapes through all directories to see which
        given/when/then formatted fixtures we have available in the current "library"
        of fixtures. It is then compared against the EXPECTED fixtures generated
        by pytest-bdd to see if we have any missing."""
        # List all files and directories in the specified directory
        files_and_directories = os.listdir(self.fixtures)

        # Filter the list to only get files (not directories)
        files = [
            f
            for f in files_and_directories
            if os.path.isfile(os.path.join(self.fixtures, f))
        ]

        defined_fixture_dict = {}
        # Print the list of files
        for file in files:
            filename = f"{self.fixtures}/{file}"
            with open(filename, "r") as fixture_file:
                for line_num, line in enumerate(fixture_file):
                    if "@given" in line or "@when" in line or "@then" in line:
                        line = self._touch_up_fixture_for_comparison(line)
                        line_key = f"{filename}:{line_num}"
                        defined_fixture_dict[line_key] = line
        return defined_fixture_dict

    @timebudget
    def _attempt_fixture_match(self, fixture):
        missing_fixtures = False
        top_best_match = ""
        top_best_match_parsed = None
        top_similarity = 0
        top_loc = ""
        for fixture_location, defined_fixture in self._defined_fixtures.items():
            fixture_type = fixture.split("(")[0]
            defined_fixture_type = defined_fixture.split("(")[0]

            fixture = fixture.replace('"', "")
            fixture = fixture.replace("'", "")
            defined_fixture = defined_fixture.replace('"', "")
            defined_fixture = defined_fixture.replace("'", "")

            similar_type = False
            if fixture_type == "@then" and defined_fixture_type == "@then":
                similar_type = True
            elif (
                fixture_type == "@when"
                or fixture_type == "@given"
                and defined_fixture_type != "@then"
            ):  # noqa: E501
                similar_type = True
            if similar_type:
                if "{}" in defined_fixture:
                    temp = self._replace_placeholder(fixture, defined_fixture)
                    if temp is not None:
                        fixture_similarity = fuzz.ratio(fixture, temp)
                        if fixture_similarity > top_similarity:
                            top_best_match_parsed = temp
                            top_best_match = defined_fixture
                            top_similarity = fixture_similarity
                            top_loc = fixture_location
                else:
                    fixture_similarity = fuzz.ratio(fixture, defined_fixture)
                    if fixture_similarity > top_similarity:
                        top_similarity = fixture_similarity
                        top_best_match = defined_fixture
                        top_loc = fixture_location
        if top_best_match_parsed is not None:
            differences = [
                (char1, char2)
                for char1, char2 in zip(fixture, top_best_match_parsed)
                if char1 != char2
            ]
        else:
            differences = [
                (char1, char2)
                for char1, char2 in zip(fixture, top_best_match)
                if char1 != char2
            ]

        if len(differences) > 0:
            missing_fixtures = True
            logging.info(f"\tSimilar fixture found with {top_similarity}% match:")
            logging.info(f"\t\tNeeded fixture: \t\t{fixture}")
            if top_best_match_parsed is not None:
                logging.info(f"\t\tDefined fixture: \t\t{top_loc} - {top_best_match}")
                logging.info(f"\t\t\tParsed fixture: \t\t{top_best_match_parsed}")
            else:
                logging.info(f"\t\tDefined fixture: \t\t{top_loc} - {top_best_match}")
        return missing_fixtures

    def _validate_generated_feature_against_pytest_fixtures(self, feature):
        if not os.path.exists(f"{self.out_dir}/{feature}"):
            raise OSError(f"Cannot validate {feature} as it is not found on disk")
        generated = self._generate_pytestbdd_for_feature(feature)

        fixtures_under_test = self._collect_needed_fixtures(generated)
        if len(self._defined_fixtures):
            logging.info(f"Checking fixture existence for {feature}")
            missing_fixtures = False
            for fixture in fixtures_under_test:
                if fixture not in self._defined_fixtures:
                    if self._attempt_fixture_match(fixture):
                        logging.error(
                            f"Feature {feature} - No matching defined fixture for"
                            + f'"{fixture}" is implemented under "fixtures"'
                        )
                        missing_fixtures = True

                else:
                    missing_fixtures = True
                    logging.error(
                        f'No matching defined fixture for "{fixture}" '
                        + 'is implemented under "fixtures"'
                    )
        else:
            logging.error(f"No defined fixtures found. needed: {fixtures_under_test}")
            missing_fixtures = True

        if not missing_fixtures:
            logging.debug("All fixtures present")
        else:
            raise MissingFixturesError("Missing Fixture definitions")

    @timebudget
    def _generate_pytestbdd_for_feature(self, feature):
        output = subprocess.run(
            ["pytest-bdd", "generate", f"{self.out_dir}/{feature}"], capture_output=True
        )
        return StringIO(output.stdout.decode("utf-8")).getvalue()

    def _write_pytestbdd_runner_file_for_feature(self, feature):
        generated = self._generate_pytestbdd_for_feature(feature)
        scenario_runners = self._parse_scenario_runners_from_pytestbdd(generated)
        os.makedirs(self.out_dir, exist_ok=True)
        test_name = feature.replace(".feature", "").lower().replace(" ", "_")
        test_filename = "test_" + test_name + ".py"
        with open(f"{self.out_dir}/{test_filename}", "w") as test_file:
            for i in scenario_runners:
                test_file.write(f"{i}\n")

    def _get_list_of_feature_files(self):
        if self.out_dir is None:
            raise OrderOfOperationsError(
                f"You have not yet populated the features, \
                    or written out the feature files for {self.plan_id}"
            )
        try:
            files = [
                f
                for f in os.listdir(self.out_dir)
                if os.path.isfile(os.path.join(self.out_dir, f))
            ]
        except FileNotFoundError:
            raise OrderOfOperationsError(
                f"You have not yet populated the features, \
                    or written out the feature files for {self.plan_id} \
                        in directory {self.out_dir}"
            )

        if not len(files):
            raise OrderOfOperationsError(
                f"No feature files to generate runners for plan {self.plan_id}"
            )
        return files

    def validate_pytestbdd_runners_against_feature_files(self):
        self._defined_fixtures = self._collect_defined_fixtures()
        for feature in self._get_list_of_feature_files():
            self._validate_generated_feature_against_pytest_fixtures(feature)
        if self._profile:
            timebudget.report(reset=True)

    def write_pytestbdd_runners(self):
        for feature in self._get_list_of_feature_files():
            self._write_pytestbdd_runner_file_for_feature(feature)

    @timebudget
    def _open_ado_connection(self):
        """This subroutine uses azure-devops APIs to connect to ADO"""
        if self.pat is None:
            raise Exception("ADO Personal Access Token (PAT) is not set.")
        self.credentials = BasicAuthentication("", self.pat)
        self.connection = Connection(
            base_url=self.organization_url, creds=self.credentials
        )

    def _get_ado_clients(self):
        """This subroutine gets all the different clients needed
        to interact with ADO via REST APIs"""
        clients = self.connection.clients

        self.test_plan_client = clients.get_test_plan_client()
        self.test_plan_client: TestPlanClient

        self.test_client = clients.get_test_client()
        self.test_client: TestClient

        self.witc = clients.get_work_item_tracking_client()
        self.witc: WorkItemTrackingClient

        self.witpc = clients.get_work_item_tracking_process_client()
        self.witpc: WorkItemTrackingProcessClient

    @timebudget
    def _get_azure_test_case_valid_states(self):
        """This subroutine queries the ADO project to get all the phases
        of the test case lifecycle. These are used in conjunction
        with "Additional States" to know what test cases, if any,
        to use for this test run."""

        processes = self.witpc.get_list_of_processes(expand="projects")
        for process in processes:
            if process.projects and self.project in [
                project.name for project in process.projects
            ]:
                logging.debug(f"{self.project} process name: {process.name}")
                work_item_types = self.witpc.get_process_work_item_types(
                    process.type_id, expand="states"
                )
                for work_item_type in work_item_types:
                    if "Test Case" == work_item_type.name:
                        if work_item_type.states:
                            self.valid_states = [
                                state.name for state in work_item_type.states
                            ]

    @timebudget
    def get_azure_test_plan(self):
        """This subroutine gets the ADO Rest API object TestPlan
        given a test plan ID"""
        if self.project is not None and self.plan_id is not None:
            self.test_plan = self.test_plan_client.get_test_plan_by_id(
                project=self.project, plan_id=self.plan_id
            )
            self.test_plan: TestPlan
        else:
            raise OrderOfOperationsError(
                "You have not configured a project and Test Plan ID"
            )

    @timebudget
    def _get_azure_test_suites(self):
        """this subroutine populates self._azure_test_suites with
        a list of ADO REST API TestSuite objects"""
        temp = self.test_plan_client.get_test_suites_for_plan(
            project=self.project, plan_id=self.plan_id
        )
        if not len(temp):
            raise NoTestSuiteError(
                f"Found no test suites for {self.plan_id} under project{self.project}"
            )
        for test_suite in temp:
            test_suite: TestSuite  # typehinting for autocompletion

            if test_suite.name != self.test_plan.name:
                logging.info(f"Adding test suite {test_suite.name} to {self.plan_id}")
                self._azure_test_suites.append(test_suite)
        if not len(self._azure_test_suites):
            raise NoTestSuiteError(
                f"Found no populated test suites for {self.plan_id} under {self.project}"
            )

    @timebudget
    def _get_azure_test_cases_for_test_suite(self, test_suite_id):
        """this subroutine popluates a list of ADO
        REST API TestCase objects"""
        return self.test_client.get_test_cases(
            project=self.project, plan_id=self.plan_id, suite_id=test_suite_id
        )

    def _populate_bdd_features_from_azure_test_suites(self):
        """This assumes _get_azure_test_suites has been called,
        and loops through the suite, checking to see if
        they are in a BDD test plan list, and adding them
        if they are not"""
        for test_suite in self._azure_test_suites:
            feature_names = (
                [feature.name for feature in self.bdd_tp.features]
                if self.bdd_tp.features
                else []
            )
            if test_suite.name not in feature_names:
                logging.info(f"Adding {test_suite.name} to {self.plan_id} features")
                feature = Feature()
                feature.name = test_suite.name
                feature.id = test_suite.id
                feature.revision = test_suite.revision
                feature.background = None
                self._populate_bdd_scenarios_from_azure_test_suite(feature, test_suite)
                self.bdd_tp.features.append(feature)

    def _populate_bdd_scenarios_from_azure_test_suite(
        self, feature: Feature, test_suite: TestSuite
    ):
        """this subroutine assumes the test suite has been populated.
        it then loops through this test suite, and builds up an
        internal self.bdd_Test_plan"""
        test_cases = self._get_azure_test_cases_for_test_suite(test_suite.id)
        if len(test_cases):
            # first we need to build an array of the the IDs
            #  of all the test cases under this test suite
            ids = []
            for test_case in test_cases:
                test_case: SuiteTestCase
                if test_case.test_case.id not in ids:
                    ids.append(test_case.test_case.id)

            # next, with that array of IDs, we can get the more
            # generic ADO work items for those IDs, and then
            # loop through those work items, converting them to
            # BDD style "scenarios"
            scenario_work_items = self.witc.get_work_items(
                ids=ids, project=self.project
            )
            for scenario_work_item in scenario_work_items:
                scenario_work_item: WorkItem

                # convert this ADO work item to a BDD Scenario.

                # if it has shared parameters, that means we treat it as a
                # scenario outline, which will have an examples table.
                is_scenario_outline = self._has_params(scenario_work_item)

                # note that the contents of these shared parameters will come
                # from a different ADO query
                if is_scenario_outline:
                    self._populate_shared_parameter_ids(scenario_work_item)

                # if it is called "background" that is a special title we are using
                # to provide shared steps across all scenarios in the current feature
                is_background = self._is_work_item_background(scenario_work_item)

                tags = self._get_work_item_tags(scenario_work_item)

                scenario = Scenario()
                scenario.name = scenario_work_item.fields["System.Title"]
                scenario.id = scenario_work_item.id
                scenario.revision = scenario_work_item.rev
                scenario.tags = tags
                scenario.is_outline = is_scenario_outline
                scenario.is_background = is_background
                scenario.ado_work_item = scenario_work_item

                if is_scenario_outline:
                    self._populate_nonshared_parameters(scenario, scenario_work_item)

                scenario.steps = self._populate_steps_for_work_item(scenario_work_item)
                if is_background:
                    feature.background = scenario
                else:
                    feature.scenarios.append(scenario)

        else:
            logging.warning(f"No test cases in suite: {test_suite.name}")

    def _is_work_item_background(self, work_item: WorkItem):
        return work_item.fields["System.Title"] == "Background"

    def _get_work_item_tags(self, work_item: WorkItem):
        tags = []
        if "System.Tags" in work_item.fields:
            tags = work_item.fields["System.Tags"]
            tags = tags.replace(" ", "")
            tags = tags.split(";")
            tags = [tag for tag in tags if tag != ""]
        return tags

    @timebudget
    def _get_azure_shared_params(self):
        """This subroutine fetches shared parameters from ADO given an ID
        and then puts those shared parameters in the self_bdd_test_plan
        if they're not already populated"""
        if self.bdd_tp.shared_parameters:
            ids = list(self.bdd_tp.shared_parameters.keys())
            shared_param_items = self.witc.get_work_items(ids=ids, project=self.project)

            for shared_param_item in shared_param_items:
                shared_param_item: WorkItem
                id = shared_param_item.id
                content = shared_param_item.fields["Microsoft.VSTS.TCM.Parameters"]

                soup = BeautifulSoup(content, "html.parser")

                for kvp in soup.find_all("kvp"):
                    key = f'@{kvp.get("key")}'

                    # special handling for ADOs weird thing where it
                    # converts leading integers to an ascii code thing
                    if key.startswith("@_x00"):
                        phrases = key.split("_")
                        ascii_char = chr(int(phrases[1][1:], 16))
                        key = f'@{ascii_char}{"_".join(phrases[2:])}'

                    # now, at the last-responsible-moment, remove the "@"
                    # from the key, because that is only useful to azure,
                    # not to pytest-bdd
                    key = key.replace("@", "")

                    if key not in self.bdd_tp.shared_parameters[id].parameters:
                        self.bdd_tp.shared_parameters[id].parameters[key] = []
                    self.bdd_tp.shared_parameters[id].parameters[key].append(
                        kvp.get("value")
                    )

    def _has_shared_params(self, work_item: WorkItem):
        """This subroutine checks to see if a work item has shared parameters
        associated with it"""
        if "Microsoft.VSTS.TCM.LocalDataSource" in work_item.fields and len(
            work_item.fields["Microsoft.VSTS.TCM.LocalDataSource"]
        ):
            try:
                params_fields = json.loads(
                    work_item.fields["Microsoft.VSTS.TCM.LocalDataSource"]
                )
            except json.JSONDecodeError:
                # a non-shared param, likely. process elsewhere.
                return False
            if len(params_fields["parameterMap"]):
                return True
        return False

    def _populate_shared_parameter_ids(self, work_item: WorkItem):
        try:
            params_fields = json.loads(
                work_item.fields["Microsoft.VSTS.TCM.LocalDataSource"]
            )
            for i in params_fields["parameterMap"]:
                sharedParamsId = i["sharedParameterDataSetId"]
                if sharedParamsId not in self.bdd_tp.shared_parameters:
                    self.bdd_tp.shared_parameters[sharedParamsId] = SharedParameters()
                    self.bdd_tp.shared_parameters[
                        sharedParamsId
                    ].revision = work_item.rev
        except json.JSONDecodeError:
            # a non-shared param, likely. process elsewhere.
            pass
        except KeyError:
            logging.warning(
                f"{work_item.id} has an unexpectedly empty parameters table"
            )
            # this is PROBABLY an empty non-shared parameter,
            # but lets handle it elsewhere.
            pass

    def _has_params(self, work_item: WorkItem):
        if "Microsoft.VSTS.TCM.Parameters" in work_item.fields and len(
            work_item.fields["Microsoft.VSTS.TCM.Parameters"]
        ):
            parameter_table = work_item.fields["Microsoft.VSTS.TCM.Parameters"]
            try:
                table = ET.fromstring(parameter_table)
            except ET.ParseError:
                table = parameter_table
            if len(table):
                return True
        if "Microsoft.VSTS.TCM.LocalDataSource" in work_item.fields and len(
            work_item.fields["Microsoft.VSTS.TCM.LocalDataSource"]
        ):
            try:
                params_fields = json.loads(
                    work_item.fields["Microsoft.VSTS.TCM.LocalDataSource"]
                )
                if len(params_fields["parameterMap"]):
                    return True
            except json.JSONDecodeError:
                pass
        return False

    def _populate_nonshared_parameters(self, scenario: Scenario, work_item: WorkItem):
        try:
            nonshared_parameter_table = work_item.fields[
                "Microsoft.VSTS.TCM.Parameters"
            ]
        except KeyError:
            # a shared param, likely. process elsewhere
            return
        try:
            parameter_data_source = work_item.fields[
                "Microsoft.VSTS.TCM.LocalDataSource"
            ]
        except KeyError:
            raise InvalidParameterError(
                f"Non-Shared parameter on {work_item.id} has no values"
            )
        try:
            params = ET.fromstring(nonshared_parameter_table).findall("param")
        except ET.ParseError:
            # its just a string ID, shared param likely. process elsewhere.
            return
        try:
            tables = ET.fromstring(parameter_data_source).findall("Table1")
        except ET.ParseError:
            # a shared param, likely. process elsewhere
            return
        if not len(tables):
            raise InvalidParameterError(
                "non-shared parameter on {work_item.id} has no table contents"
            )
        for param in params:  # not sure how fragile this is...
            param_name = param.attrib["name"]
            for table in tables:
                for elem in table:
                    tag = elem.tag  # don't need the @ here
                    if param_name == tag:
                        text = elem.text
                        if tag not in scenario.non_shared_parameters:
                            scenario.non_shared_parameters[tag] = []
                        scenario.non_shared_parameters[tag].append(text)
                        break

    def _collect_steps_and_comprefs(self, element):
        """This subroutine iterates through a test case and finds all sets.
        It is also recursive - if a step references steps in itself, it dives
        into those"""
        elements_list = []

        for child in element:
            if child.tag == "step":
                elements_list.append(child)
            elif child.tag in ("compref", "steps"):
                elements_list.append(child)
                elements_list.extend(self._collect_steps_and_comprefs(child))

        return elements_list

    def _ado_to_pytest_bdd_notation(self, content: str, parent_id: int):
        # replace the azure devops "@" notation for shared parameters
        # with the pytest-bdd <variable> notation
        words = content.split(" ")
        if words[0].lower() not in self.valid_starters:
            raise InvalidGherkinError(
                f'{parent_id} step "{content}" does not start with one of {self.valid_starters}'
            )  # noqa: E501
        for word in words:
            if "@" in word:
                updated_word = word.replace("@", "")
                if "<" == updated_word[0] or ">" == updated_word[-1]:
                    logging.warning(
                        f"You don't need to put the <> characters in your shared parameter ({updated_word}) - that is added programatically ({parent_id})"
                    )  # noqa: E501
                if updated_word[0] != "<":
                    updated_word = "<" + updated_word
                if updated_word[-1] != ">":
                    updated_word = updated_word + ">"
                logging.debug(f"updating {word} to {updated_word}")
                content = content.replace(word, updated_word)
        return content

    def _populate_steps_for_work_item(self, work_item: WorkItem):
        """This subroutine iterates through all the steps associated with
        an ADO work item (Specifically a test case type of work item).
        It then returns a dictionary containing those steps."""
        step_title = "Microsoft.VSTS.TCM.Steps"
        if step_title in work_item.fields:
            steps = work_item.fields[step_title]
        else:
            raise InvalidStepError(
                f"{work_item.id}::{work_item.fields['System.Title']} has no steps"
            )

        # now that we know this work item has some steps, lets see if they are
        # shared or unshared
        steps_for_item = []
        root = ET.fromstring(steps)
        all_elements = self._collect_steps_and_comprefs(root)
        for element in all_elements:
            if element.tag == "compref":
                # compref inidicated a shared step, so add a placeholder for now
                shared_step_id = element.get("ref")
                step = Step()
                step.id = shared_step_id
                step.text = ""

                # blank - will be linked later via a batched fetch
                steps_for_item.append(step)
            elif element.tag == "step":
                # this is a non-shared step. lets read it now.
                parameterized_string_elements = element.findall("parameterizedString")
                if parameterized_string_elements[0] is not None:
                    soup = BeautifulSoup(
                        parameterized_string_elements[0].text, "html.parser"
                    )

                    # Find and extract the text content within the <P> element
                    p = soup.find("p")
                    if p:
                        content = p.get_text().strip()  # remove trailing whitespace
                        step = Step()
                        step.id = None
                        step.revision = work_item.rev
                        content = self._ado_to_pytest_bdd_notation(
                            content, work_item.id
                        )
                        step.text = content
                        steps_for_item.append(step)
                        logging.warning(
                            f"Test case is using a non-shared step when shared steps are recommended: {work_item.id}::{work_item.fields['System.Title']}"
                        )  # noqa: E501
        return steps_for_item

    def _parse_shared_step_content(self, shared_step_item: WorkItem):
        """This subroutine takes a shared step work item and parses out
        every step from that work item into a list to populate step content with"""
        step_content = []
        title = shared_step_item.fields["System.Title"]
        if "Microsoft.VSTS.TCM.Steps" in shared_step_item.fields:
            possible_sub_steps = shared_step_item.fields["Microsoft.VSTS.TCM.Steps"]
            sub_root = ET.fromstring(possible_sub_steps)

            sub_all_elements = self._collect_steps_and_comprefs(sub_root)
            contents_found = False
            if len(sub_all_elements) > 1:
                first_step = True

                for sub_step in sub_all_elements:
                    # content = self._parse_substep(sub_step)
                    parameterized_string_elements = sub_step.findall(
                        "parameterizedString"
                    )
                    if parameterized_string_elements[0] is not None:
                        soup = BeautifulSoup(
                            parameterized_string_elements[0].text, "html.parser"
                        )

                        # Find and extract the text content within the <P> element
                        p = soup.find("p")
                        if p:
                            content = p.get_text().strip()
                            if content != "":
                                content = self._ado_to_pytest_bdd_notation(
                                    content, shared_step_item.id
                                )
                                contents_found = True
                                if first_step:
                                    step_content.append(
                                        f"# Start Shared Steps for {shared_step_item.id}: {title} Revision {shared_step_item.rev}"
                                    )  # noqa: E501
                                    first_step = False
                                step_content.append("\t\t" + content)
                            else:
                                logging.warning(f"Empty step in {title}")
                if not first_step:
                    step_content.append(
                        f"\t\t# End Shared Steps for {shared_step_item.id}"
                    )
                if not contents_found:
                    logging.warning(
                        f"Full contents of substep is empty for:\
    {title}"
                    )
            else:
                if len(sub_all_elements) == 1:
                    # compare the element against the title?
                    parameterized_string_elements = sub_all_elements[0].findall(
                        "parameterizedString"
                    )
                    if parameterized_string_elements[0] is not None:
                        soup = BeautifulSoup(
                            parameterized_string_elements[0].text, "html.parser"
                        )

                        # Find and extract the text content within the <P> element
                        p = soup.find("p")
                        if p:
                            content = p.get_text().strip()
                            if len(content):
                                if content != title:
                                    content = self._ado_to_pytest_bdd_notation(
                                        content, shared_step_item.id
                                    )
                                    step_content.append(
                                        f"# Shared step for {shared_step_item.id}_Revision_{shared_step_item.rev}: {title}"
                                    )  # noqa: E501
                                    step_content.append("\t\t" + content)
                                    return step_content
                            else:
                                # likely a "then" where there is an expected result
                                content = self._ado_to_pytest_bdd_notation(
                                    title, shared_step_item.id
                                )
                                return content
                content = self._ado_to_pytest_bdd_notation(title, shared_step_item.id)
                return content
            return step_content
        else:
            content = self._ado_to_pytest_bdd_notation(title, shared_step_item.id)
            return [content]

    @timebudget
    def _get_azure_shared_steps(self):
        """This subroutine takes a populated test plan and
        checks every step for a shared step ID and populates
        the step content accordingly. Batches all shared step items
        into a single call"""
        ids = []
        for feature in self.bdd_tp.features:
            if feature.background:
                for step in feature.background.steps:
                    if step.id is not None and step.id not in ids:
                        ids.append(step.id)
            for scenario in feature.scenarios:
                for step in scenario.steps:
                    if step.id is not None and step.id not in ids:
                        ids.append(step.id)

        if len(ids):
            shared_step_items = self.witc.get_work_items(ids=ids, project=self.project)
            for shared_step_item in shared_step_items:
                id = shared_step_item.id
                if id in self._shared_steps:
                    logging.info(f"already fetched shared step {id}")
                else:
                    self._shared_steps[id] = self._parse_shared_step_content(
                        shared_step_item
                    )
        else:
            logging.warning(f"No shared step IDs for plan {self.plan_id}")

    def _link_shared_steps_back_to_bdd_scenarios(self):
        """there are no ADO queries here. This subroutine searches through all of the
        placeholders in existing scenarios and populates them with the
        shared step contents that have previously been fetched."""
        if len(self._shared_steps):
            for feature in self.bdd_tp.features:
                if feature.background:
                    for (
                        shared_step_id,
                        shared_step_contents,
                    ) in self._shared_steps.items():  # noqa: E501
                        for i in range(0, len(feature.background.steps)):
                            if (
                                feature.background.steps[i].id
                                and int(feature.background.steps[i].id)
                                == shared_step_id
                            ):  # noqa: E501
                                if isinstance(shared_step_contents, str):
                                    feature.background.steps[
                                        i
                                    ].text = shared_step_contents  # noqa: E501
                                else:
                                    feature.background.steps[i].text = "\n".join(
                                        shared_step_contents
                                    )  # noqa: E501
                for scenario_i in range(0, len(feature.scenarios)):
                    for i in range(0, len(feature.scenarios[scenario_i].steps)):  # noqa: E501
                        for (
                            shared_step_id,
                            shared_step_contents,
                        ) in self._shared_steps.items():  # noqa: E501
                            if (
                                feature.scenarios[scenario_i].steps[i].id
                                and int(feature.scenarios[scenario_i].steps[i].id)
                                == shared_step_id
                            ):  # noqa: E501
                                if isinstance(shared_step_contents, str):
                                    feature.scenarios[scenario_i].steps[
                                        i
                                    ].text = shared_step_contents  # noqa: E501
                                else:
                                    feature.scenarios[scenario_i].steps[
                                        i
                                    ].text = "\n".join(shared_step_contents)  # noqa: E501

    @timebudget
    def generate_usage_graph(self):
        """This subroutine will generate an image of all the features
        a plan uses, and all teh scnearios in those features, and then
        all of the given/when/then clauses used by those scenarios.  Odds are
        its a messy image, but it might highlight areas where we're getting
        a lot of re-use?"""
        if self.bdd_tp.features:
            graph = pydot.Dot(
                f"Test Suite {self.plan_id}", graph_type="digraph", bgcolor="white"
            )
            graph.add_node(pydot.Node(self.plan_id, label=self.plan_id, shape="box"))

            for feature in self.bdd_tp.features:
                feature_id = feature.id
                if not graph.get_node(feature_id):
                    graph.add_node(
                        pydot.Node(feature_id, label=feature.name, shape="box")
                    )
                if not graph.get_edge([self.plan_id, feature_id]):
                    graph.add_edge(pydot.Edge(self.plan_id, feature_id))
                for scenario in feature.scenarios:
                    scenario_name = scenario.name
                    scenario_id = scenario.id
                    if not graph.get_node(scenario_id):
                        graph.add_node(
                            pydot.Node(scenario_id, label=scenario_name, shape="box")
                        )
                    if not graph.get_edge([feature_id, scenario_id]):
                        graph.add_edge(pydot.Edge(feature_id, scenario_id))
                    for step in [step.text for step in scenario.steps]:
                        if not graph.get_node(step):
                            graph.add_node(pydot.Node(step, label=step, shape="box"))
                        if not graph.get_edge([scenario_id, step]):
                            graph.add_edge(pydot.Edge(scenario_id, step))

            graph.write_svg(f"{self.plan_id}.svg")
        else:
            raise OrderOfOperationsError("No Features to graph!")

    def validate(self):
        # right now this just prints to stdout
        # we should figure out a couple things with it.
        # 1) redirect the stdout to logging, or another more robust
        #       recording destination
        # 2) return an error code if any linting issues are found
        #       so the command can fail accordingly.

        GherkinLinter(Path(self.out_dir)).run()
        logging.info("validation complete!")
