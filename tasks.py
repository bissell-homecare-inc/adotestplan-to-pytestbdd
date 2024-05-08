import os

from invoke import task


@task(aliases=["b"])
def build(c):
    c.run("python3 -m poetry build", pty=True)


@task(aliases=["t"])
def tests(c, log_level="error", tests: str = None):
    test_arg = f" -k {tests} " if tests else ""
    c.run(
        f"poetry run coverage run -m pytest \
            --junitxml=test_results.xml \
            --log-cli-level={log_level} {test_arg}",
        pty=True,
    )


@task(aliases=["c"])
def check(c):
    c.run("pre-commit run --all-files", pty=True)


@task
def install(c):
    c.run("poetry install -vvv")


@task(install)
def update(c):
    c.run("poetry update -vvv")


@task
def delete_test_work_items(c):
    import base64

    import requests
    from azure.devops.connection import Connection
    from azure.devops.exceptions import AzureDevOpsServiceError
    from azure.devops.v7_0.core.core_client import CoreClient
    from azure.devops.v7_0.test_plan.test_plan_client import TestPlanClient
    from azure.devops.v7_0.work_item_tracking import Wiql, WorkItemTrackingClient
    from dotenv import load_dotenv
    from msrest.authentication import BasicAuthentication

    load_dotenv()  # take environment variables from .env.
    pat = os.getenv("TEST_PAT")
    credentials = BasicAuthentication("", pat)
    test_url = os.getenv("TEST_URL")
    connection = Connection(base_url=test_url, creds=credentials)
    clients = connection.clients

    witc = clients.get_work_item_tracking_client()
    witc: WorkItemTrackingClient

    cc = clients.get_core_client()
    cc: CoreClient

    tpc = clients.get_test_plan_client()
    tpc: TestPlanClient

    proj = os.getenv("TEST_PROJ")

    if (
        input(
            f'are you sure you want to delete ALL test-related work items from within the "{proj}" project under {test_url} (y/n)'
        )
        != "y"
    ):  # noqa: E501
        return

    projects = cc.get_projects()
    project_id = projects[0].id

    wiql = Wiql()

    wiql.query = (
        "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'Test Plan'"  # noqa: E501
    )
    for tp in witc.query_by_wiql(wiql).work_items:
        for ts in tpc.get_test_suites_for_plan(project=proj, plan_id=tp.id):
            try:
                tpc.delete_test_suite(proj, plan_id=tp.id, suite_id=ts.id)
            except AzureDevOpsServiceError:
                pass  # take care of it elsewhere
        try:
            tpc.delete_test_plan(proj, tp.id)
        except AzureDevOpsServiceError:
            pass

    wiql.query = (
        "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'Test Suite'"  # noqa: E501
    )
    for ts in witc.query_by_wiql(wiql).work_items:
        # i dont know why, but 39 seems to be the placeholder for unparented test suites
        tpc.delete_test_suite(proj, plan_id=39, suite_id=ts.id)

    wiql.query = (
        "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'Test Case'"  # noqa: E501
    )
    for tc in witc.query_by_wiql(wiql).work_items:
        tpc.delete_test_case(proj, tc.id)

    wiql.query = (
        "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'Shared Steps'"  # noqa: E501
    )
    for shared_step in witc.query_by_wiql(wiql).work_items:
        url = f'{os.getenv("TEST_URL")}/{project_id}/_apis/test/sharedstep/{shared_step.id}?api-version=5.0-preview.1'  # noqa: E501
        headers = {}
        headers["Authorization"] = "Basic " + base64.b64encode(
            str.encode(f"user:{pat}")
        ).decode("utf-8")
        response = requests.delete(url=url, headers=headers)
        if response.status_code != 204:
            print(
                f"Issue deleting shared step {shared_step.id} returned: {response.status_code} : {response.reason}"
            )  # noqa: E501

    wiql.query = "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'Shared Parameter'"  # noqa: E501
    for shared_parameter in witc.query_by_wiql(wiql).work_items:
        url = f'{os.getenv("TEST_URL")}/{project_id}/_apis/test/SharedParameter/{shared_parameter.id}?api-version=5.0-preview.1'  # noqa: E501
        headers = {}
        headers["Authorization"] = "Basic " + base64.b64encode(
            str.encode(f"user:{pat}")
        ).decode("utf-8")
        response = requests.delete(url=url, headers=headers)
        if response.status_code != 204:
            print(
                f"Issue deleting shared parameter {shared_step.id} "
                + f"returned: {response.status_code} : {response.reason}"
            )


@task(aliases=["s"])
def generate_test_work_items(c):
    """
    Establish the basic test scenarios needed to run unit tests.
    The idea is you can run this once, and then run unit tests as necessary.
    This avoids burning through work item IDs unnecessarily - the same work
    items can be used over and over for testing.

    This test then also sets up an environment/configuration file the tests
    can read from to know which work items to use for which tests.
    """
    from azure.devops.connection import Connection
    from azure.devops.v7_0.test_plan.test_plan_client import TestPlanClient
    from azure.devops.v7_0.work_item_tracking import WorkItemTrackingClient
    from dotenv import load_dotenv
    from msrest.authentication import BasicAuthentication

    load_dotenv()  # take environment variables from .env.
    pat = os.getenv("TEST_PAT")

    credentials = BasicAuthentication("", pat)
    url = os.getenv("TEST_URL")
    connection = Connection(base_url=url, creds=credentials)
    clients = connection.clients

    witc = clients.get_work_item_tracking_client()
    witc: WorkItemTrackingClient

    tpc = clients.get_test_plan_client()
    tpc: TestPlanClient

    proj = os.getenv("TEST_PROJ")
    print(f"TEST_PAT={pat}")
    print(f"TEST_URL={url}")
    print(f"TEST_PROJ={proj}")

    # Admittedly, this is somewhat of a half-baked approach.
    # what we're doing here is basically round-tripping the utility
    # this repo provides (or, put differently, reversing it)...
    # this is going from a .feature file, or something like it,
    # to an ADO test plan.  So this could probably be formalized
    # a bit more and made into its own utility in and of itself.
    test_plans = {
        "empty": {"suites": {}},
        "non-empty": {
            "suites": {
                "Background with Shared Steps": {
                    "Background": {
                        "shared steps": ["Given a shared step"],
                        "steps": [],
                    },
                    "Scenario for Background with a Shared Step": {
                        "steps": ["Given the background has a shared step"]
                    },
                },
                "Background Suite": {
                    "Background": {"steps": ["Given the background"]},
                    "Scenario with a Background": {
                        "steps": ["Given a background has already occurred"]
                    },
                },
                "Normal Suite": {
                    "Scenario A": {  # does this one add anything that the previous two didn't do?  # noqa: E501
                        "steps": ["Given Hello", "When World", "Then !"]
                    }
                },
            }
        },
        "shared-steps-and-shared-parameters": {
            "suites": {
                "Shared Steps and Shared Params Suite": {
                    "Shared Param and Shared Step Scenario": {
                        "shared steps": [
                            "Given a shared step",
                        ],
                        "steps": ["When a non shared step", "Then @Parameter1"],
                        "shared parameters": {"Shared Parameters 1": ["Parameter1"]},
                    },
                    "Longer Shared Step Scenario": {
                        "shared steps": ["Given a longer shared step"]
                    },
                    "Multi-Value Shared Param Scenario": {
                        "steps": ["Given @MultiValueParameter"],
                        "shared parameters": {
                            "Shared Parameters 1": ["MultiValueParameter"]
                        },
                    },
                    "Multiple Multi-Value Shared Param Scenario": {
                        "steps": [
                            "Given @MultiValueParameter and @AnotherMultiValueParameter"
                        ],
                        "shared parameters": {
                            "Shared Parameters 1": [
                                "MultiValueParameter",
                                "AnotherMultiValueParameter",
                            ]
                        },
                    },
                    "Scenario with a Single Shared Step": {
                        "shared steps": ["Given a single step shared step"]
                    },
                }
            }
        },
        "non-shared-parameters": {
            "suites": {
                "Non-Shared Param Suite": {
                    "Non-shared Param Scenario": {
                        "steps": ["Given @NonSharedParameter1"],
                        "parameters": {
                            "NonSharedParameter1": ["this is a non-shared parameter"]
                        },
                    },
                    "Multi-Value Non-Shared Param Scenario": {
                        "steps": ["Given @MultiValueNonSharedParameter"],
                        "parameters": {
                            # add the wrinkle of mixed type here
                            "MultiValueNonSharedParameter": ["one", "two", "three", 4]
                        },
                    },
                    "Multiple Multi-Value Non-Shared Parameter Scenario": {
                        "steps": [
                            "Given @MultiValueNonSharedParameterOne and @MultiValueNonSharedParameterTwo"  # noqa: E501
                        ],
                        "parameters": {
                            "MultiValueNonSharedParameterOne": [1, 2, 3, 4],
                            "MultiValueNonSharedParameterTwo": [5, 6, 7, 8],
                        },
                    },
                }
            }
        },
        "invalid-gherkin": {
            "suites": {
                "invalid steps": {
                    "Scenario where steps don't follow Given-When-Then Syntax Rules": {
                        "steps": ["This is not a valid gherkin step"]
                    }
                }
            }
        },
        "empty-parameters": {
            "suites": {
                "empty non-shared parameter suite": {
                    "Scenario with valid param name but param is empty": {
                        "steps": ["Given a valid but empty @Parameter1"],
                        "parameters": {"Parameter1": []},
                    }
                }
            }
        },
    }

    shared_steps = {
        "Given a shared step": "Given a shared step",
        "Given a single step shared step": [
            "Given the single step shared step is different than the title"
        ],
        "Given a longer shared step": [
            "Given Longer Shared Step 1",
            "Given Longer Shared Step 2",
            "Given Longer Shared Step 3",
        ],
    }

    shared_step_ids = {}

    shared_parameters = {
        "Shared Parameters 1": {
            "Parameter1": [1],
            "MultiValueParameter": [5, 6, 7, 8],
            "AnotherMultiValueParameter": ["one", "two", "three", "four"],
        }
    }

    # before populating the plans, create the shared parameters first,
    # and then shared steps, so the tests have something to refer to.

    # we do shared parameters first because the shared steps may refer
    # to them
    shared_parameters = populate_shared_parameters(shared_parameters, witc, proj)

    # now that shared parameters are all set we can build up shared steps.
    # we need to be able to reference the shared parameter by ID
    shared_step_ids = populate_shared_steps(shared_steps, witc, proj)

    populate_test_plans(test_plans, tpc, witc, proj, shared_parameters, shared_step_ids)


def populate_test_plans(
    test_plans, tpc, witc, proj, shared_parameters, shared_step_ids
):
    from azure.devops.v7_0.test_plan.models import (
        SuiteTestCaseCreateUpdateParameters,
        TestPlan,
        TestPlanCreateParams,
        TestSuite,
        TestSuiteCreateParams,
    )
    from azure.devops.v7_0.work_item_tracking.models import JsonPatchOperation

    for title, tp in test_plans.items():
        # Step 1 - Create the test plan
        document = [
            JsonPatchOperation(op="add", path="/fields/System.Title", value=title)
        ]
        tpcp = TestPlanCreateParams()
        tpcp.name = title
        tpwi = tpc.create_test_plan(tpcp, proj)
        env_string = f'TEST_{title.replace("-", "_").upper()}_TP={tpwi.id}'
        print(env_string)
        tpwi: TestPlan  # for type hinting and autocomplete

        for suite_title, suite in tp["suites"].items():
            # Step 2 - Create the test suite under the test plan
            document = [
                JsonPatchOperation(
                    op="add", path="/fields/System.Title", value=suite_title
                )
            ]
            tscp = TestSuiteCreateParams()
            tscp.name = suite_title
            tscp.parent_suite = tpwi.root_suite
            tscp.suite_type = "staticTestSuite"
            tswi = tpc.create_test_suite(tscp, proj, tpwi.id)
            tswi: TestSuite
            for scenario_title, scenario in suite.items():
                # Step 3 - Create the test case under the test suite
                document = [
                    JsonPatchOperation(
                        op="add", path="/fields/System.Title", value=scenario_title
                    )
                ]
                # Step 4 - Populate the test case with steps and parameters
                document = populate_parameters_for_scenario(
                    scenario, document, shared_parameters
                )
                document = populate_steps_for_scenario(
                    scenario, document, shared_step_ids
                )

                test_case_work_item = witc.create_work_item(document, proj, "Test Case")
                stccup = SuiteTestCaseCreateUpdateParameters()
                stccup.work_item = test_case_work_item
                tpc.add_test_cases_to_suite([stccup], proj, tpwi.id, tswi.id)


def populate_shared_steps(shared_steps, witc, proj):
    from xml.etree.ElementTree import Element, tostring

    from azure.devops.v7_0.work_item_tracking.models import JsonPatchOperation

    shared_step_ids = {}
    for title, shared_step_value in shared_steps.items():
        document = [
            JsonPatchOperation(op="add", path="/fields/System.Title", value=title)
        ]
        shared_step_work_item = witc.create_work_item(document, proj, "Shared Steps")
        if isinstance(shared_step_value, list):
            step_id = 1
            steps_element = Element("steps")
            steps_element.attrib["id"] = str(0)
            steps_element.attrib["last"] = str(0)
            for step in shared_step_value:
                step_element = Element("step")
                step_element.attrib["id"] = str(step_id)
                # also ValidateStep
                step_element.attrib["type"] = "ActionStep"
                string_element = Element("parameterizedString")
                string_element.attrib["isFormatted"] = "true"
                string_element.text = f"<DIV><DIV><P>{step}</P></DIV></DIV>"
                step_element.append(string_element)
                second_parameterized_string_element = Element("parameterizedString")
                second_parameterized_string_element.attrib["isFormatted"] = "true"
                second_parameterized_string_element.text = (
                    "<DIV><DIV><P><BR/></P></DIV></DIV>"  # noqa: E501
                )
                step_element.append(second_parameterized_string_element)
                steps_element.append(step_element)
                steps_element.attrib["last"] = str(step_id)
                step_id += 1

            payload = tostring(steps_element).decode("utf-8")
            document.append(
                JsonPatchOperation(
                    op="add", path="/fields/Microsoft.VSTS.TCM.Steps", value=payload
                )
            )
            shared_step_work_item = witc.update_work_item(
                document, shared_step_work_item.id, proj
            )
        shared_step_ids[title] = shared_step_work_item.id
    return shared_step_ids


def populate_shared_parameters(shared_parameters, witc, proj):
    from xml.etree.ElementTree import Element, tostring

    from azure.devops.v7_0.work_item_tracking.models import JsonPatchOperation

    for title, shared_parameter in shared_parameters.items():
        parameter_set = {"paramNames": [], "paramData": []}

        for param_name, param_contents in shared_parameter.items():
            parameter_set["paramNames"].append(param_name)
            for idx, contents in enumerate(param_contents):
                cell = {"key": param_name, "value": str(contents)}
                if len(parameter_set["paramData"]) > idx:
                    # it was already there...
                    parameter_set["paramData"][idx]["kvp"].append(cell)
                else:
                    parameter_set["paramData"].append({"kvp": [cell]})

        # now we manually build an XML string from the dict

        param_set = Element("parameterSet")
        param_names_element = Element("paramNames")
        for name in parameter_set["paramNames"]:
            name_element = Element("param")
            name_element.text = name
            param_names_element.append(name_element)
        param_set.append(param_names_element)
        param_data_element = Element("paramData")
        for idx, data_row in enumerate(parameter_set["paramData"]):
            data_row_element = Element("dataRow")
            data_row_element.attrib["id"] = str(idx + 1)
            for kvp in data_row["kvp"]:
                kvp_element = Element("kvp")
                kvp_element.attrib["key"] = str(kvp["key"])
                kvp_element.attrib["value"] = str(kvp["value"])
                data_row_element.append(kvp_element)
            param_data_element.attrib["lastId"] = str(idx + 1)
            param_data_element.append(data_row_element)
        param_set.append(param_data_element)
        payload = tostring(param_set).decode("utf-8")

        document = [
            JsonPatchOperation(op="add", path="/fields/System.Title", value=title),
            JsonPatchOperation(
                op="add", path="/fields/Microsoft.VSTS.TCM.Parameters", value=payload
            ),
        ]
        shared_parameters[title]["id"] = witc.create_work_item(
            document, proj, "Shared Parameter"
        ).id
    return shared_parameters


def populate_parameters_for_scenario(scenario, document, shared_parameters):
    from xml.etree.ElementTree import Element, tostring

    from azure.devops.v7_0.work_item_tracking.models import JsonPatchOperation

    parameters_element = Element("parameters")
    parameter_data_element = Element("NewDataSet")
    non_shared_parameters = False
    if "parameters" in scenario and len(scenario["parameters"]):
        non_shared_parameters = True
        for parameter_name, parameter_values in scenario["parameters"].items():
            param_element = Element("param")
            param_element.attrib["name"] = parameter_name
            param_element.attrib["bind"] = "default"
            parameters_element.append(param_element)
            for row_idx, value in enumerate(parameter_values):
                try:
                    value_table_element = parameter_data_element.findall("Table1")[
                        row_idx
                    ]
                    new = False
                except IndexError:
                    value_table_element = Element("Table1")
                    new = True
                value_element = Element(parameter_name)
                value_element.text = str(value)
                value_table_element.append(value_element)
                if new:
                    parameter_data_element.append(value_table_element)
                else:
                    parameter_data_element[row_idx] = value_table_element
    if parameters_element:
        parameter_name_payload = tostring(parameters_element).decode("utf-8")
        document.append(
            JsonPatchOperation(
                op="add",
                path="/fields/Microsoft.VSTS.TCM.Parameters",
                value=parameter_name_payload,
            )
        )
        parameter_data_payload = tostring(parameter_data_element).decode("utf-8")
        document.append(
            JsonPatchOperation(
                op="add",
                path="/fields/Microsoft.VSTS.TCM.LocalDataSource",
                value=parameter_data_payload,
            )
        )
    if "shared parameters" in scenario and len(scenario["shared parameters"]):
        if non_shared_parameters:
            raise ValueError(
                "you're trying to setup non-shared parameters and "
                + "shared on the same item. you can't do that."
            )
        lds = {"parameterMap": [], "rowMappingType": 0, "sharedParameterDataSetIds": []}
        parameters_element = Element("parameters")
        for shared_parameter_name in scenario["shared parameters"]:
            param_element = Element("param")
            param_element.attrib["name"] = shared_parameter_name
            param_element.attrib["bind"] = "default"
            parameters_element.append(param_element)
            id = shared_parameters[shared_parameter_name]["id"]
            lds["parameterMap"].append(
                {
                    "localParamName": shared_parameter_name,
                    "sharedParameterName": shared_parameter_name,
                    "sharedParameterDataSetId": id,
                }
            )
            lds["sharedParameterDataSetIds"].append(id)

        parameter_name_payload = tostring(parameters_element).decode("utf-8")
        document.append(
            JsonPatchOperation(
                op="add",
                path="/fields/Microsoft.VSTS.TCM.Parameters",
                value=parameter_name_payload,
            )
        )
        import json

        document.append(
            JsonPatchOperation(
                op="add",
                path="/fields/Microsoft.VSTS.TCM.LocalDataSource",
                value=json.dumps(lds),
            )
        )
    return document


def populate_steps_for_scenario(scenario, document, shared_step_ids):
    from xml.etree.ElementTree import Element, tostring

    from azure.devops.v7_0.work_item_tracking.models import JsonPatchOperation

    # reference: https://stackoverflow.com/questions/54105690/
    # how-to-add-test-steps-in-a-test-case-work-item-using-rest-api-tfs2018-python#54107054
    step_id = 1
    steps_element = Element("steps")
    steps_element.attrib["id"] = str(0)
    steps_element.attrib["last"] = str(0)
    if "steps" in scenario and len(scenario["steps"]):
        for step in scenario["steps"]:
            step_element = Element("step")
            step_element.attrib["id"] = str(step_id)
            # also ValidateStep
            step_element.attrib["type"] = "ActionStep"
            string_element = Element("parameterizedString")
            string_element.attrib["isFormatted"] = "true"
            string_element.text = f"<DIV><DIV><P>{step}</P></DIV></DIV>"
            step_element.append(string_element)
            second_parameterized_string_element = Element("parameterizedString")
            second_parameterized_string_element.attrib["isFormatted"] = "true"
            second_parameterized_string_element.text = (
                "<DIV><DIV><P><BR/></P></DIV></DIV>"  # noqa: E501
            )
            step_element.append(second_parameterized_string_element)
            steps_element.append(step_element)
            steps_element.attrib["last"] = str(step_id)
            step_id += 1

    if "shared steps" in scenario and len(scenario["shared steps"]):
        for step in scenario["shared steps"]:
            compref_element = Element("compref")
            compref_element.attrib["id"] = str(step_id)
            # now look up the ID in our shared step dictionary created earlier
            compref_element.attrib["ref"] = str(shared_step_ids[step])
            steps_element.append(compref_element)
            steps_element.attrib["last"] = str(step_id)
            step_id += 1

    if steps_element:
        payload = tostring(steps_element).decode("utf-8")
        document.append(
            JsonPatchOperation(
                op="add", path="/fields/Microsoft.VSTS.TCM.Steps", value=payload
            )
        )

    return document
