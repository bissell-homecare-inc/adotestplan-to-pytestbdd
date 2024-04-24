import os

from dotenv import load_dotenv
from pytest import fixture, raises

from adotestplan_to_pytestbdd import ADOTestPlan

root_path = os.path.dirname(os.path.realpath(__file__))

load_dotenv()  # take environment variables from .env.

org_url = os.getenv("TEST_URL")
pat = os.getenv("TEST_PAT")

proj = os.getenv("TEST_PROJ")


@fixture
def default_tp():
    tp = ADOTestPlan(organization_url=org_url, pat=pat)
    yield tp


@fixture
def unique_outdir_tp(default_tp):
    default_tp.out_dir = 'xyz'
    yield default_tp


@fixture
def empty_tp(default_tp):
    default_tp.plan_id = os.getenv("TEST_EMPTY_TP")
    default_tp.project = proj
    yield default_tp


@fixture
def populated_non_empty_tp(default_tp):
    default_tp.plan_id = os.getenv("TEST_NON_EMPTY_TP")
    default_tp.project = proj
    default_tp.out_dir = 'default_tp'
    default_tp.populate()
    yield default_tp


@fixture
def populated_and_written_tp(populated_non_empty_tp):
    populated_non_empty_tp.write_feature_files()
    populated_non_empty_tp.fixtures = os.path.join(
        root_path, 'fixtures_under_test')
    yield populated_non_empty_tp


@fixture
def missing_fixtures_tp(populated_non_empty_tp):
    populated_non_empty_tp.write_feature_files()
    populated_non_empty_tp.fixtures = os.path.join(
        root_path, 'missing_fixtures_example')
    yield populated_non_empty_tp


@fixture
def tp_with_shared_steps_and_shared_params(default_tp):
    default_tp.plan_id = os.getenv(
        "TEST_SHARED_STEPS_AND_SHARED_PARAMETERS_TP")
    default_tp.project = proj
    default_tp.out_dir = 'shared_steps_and_params'
    yield default_tp


@fixture
def tp_with_non_shared_params(default_tp):
    default_tp.plan_id = os.getenv("TEST_NON_SHARED_PARAMETERS_TP")
    default_tp.project = proj
    default_tp.out_dir = 'nonshared_params'
    yield default_tp


@fixture
def invalid_gherkin_tp(default_tp):
    default_tp.plan_id = os.getenv("TEST_INVALID_GHERKIN_TP")
    default_tp.project = proj
    default_tp.out_dir = 'invalid_gherkin_tp'
    yield default_tp


@fixture
def empty_parameters_tp(default_tp):
    default_tp.plan_id = os.getenv("TEST_EMPTY_PARAMETERS_TP")
    default_tp.project = proj
    default_tp.out_dir = 'empty_parameter_tp'
    yield default_tp


def test_init_no_profiling():
    ADOTestPlan(profile=False, organization_url=org_url, pat=pat)


def test_init_default_args():
    ADOTestPlan(organization_url=org_url, pat=pat)


def test_populate_non_configured(default_tp):
    with raises(ValueError):
        default_tp.populate()


def test_populate_empty(empty_tp):
    with raises(ValueError):
        empty_tp.populate()


def test_write_feature_files_non_configured(default_tp):
    with raises(ValueError):
        default_tp.write_feature_files()


def test_write_feature_files(populated_and_written_tp):
    populated_and_written_tp.write_feature_files()


def test_generate_scenario_runners_without_populating(unique_outdir_tp):
    with raises(FileNotFoundError):
        unique_outdir_tp.write_pytestbdd_runners()


def test_generate_scenario_runners(populated_and_written_tp):
    populated_and_written_tp.write_pytestbdd_runners()


def test_usage_graph_generation_without_populating():
    with raises(ValueError):
        ADOTestPlan(organization_url=org_url).generate_usage_graph()


def test_usage_graph_generation(populated_and_written_tp):
    populated_and_written_tp.generate_usage_graph()


def test_nonshared_params_populate(tp_with_non_shared_params):
    tp_with_non_shared_params.populate()


def test_nonshared_params_write(tp_with_non_shared_params):
    tp_with_non_shared_params.populate()
    tp_with_non_shared_params.write_feature_files()


def test_shared_params_and_shared_steps_populate(tp_with_shared_steps_and_shared_params):
    tp_with_shared_steps_and_shared_params.populate()


def test_shared_params_and_shared_steps_write(tp_with_shared_steps_and_shared_params):
    tp_with_shared_steps_and_shared_params.populate()
    tp_with_shared_steps_and_shared_params.write_feature_files()


def test_write_pytestbdd_runners(populated_and_written_tp):
    populated_and_written_tp.write_pytestbdd_runners()


def test_validate_pytestbdd_runners(populated_and_written_tp):
    populated_and_written_tp.write_pytestbdd_runners()
    populated_and_written_tp.validate_pytestbdd_runners_against_feature_files()


def test_validate_missing_fixtures(missing_fixtures_tp):
    missing_fixtures_tp.write_pytestbdd_runners()
    with raises(ValueError):
        missing_fixtures_tp.validate_pytestbdd_runners_against_feature_files()


def test_empty_parameter_raises_exception(empty_parameters_tp):
    with raises(ValueError):
        empty_parameters_tp.populate()


def test_validate_invalid_gherkin(invalid_gherkin_tp):
    with raises(ValueError):
        invalid_gherkin_tp.populate()
