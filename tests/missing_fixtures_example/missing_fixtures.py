from pytest_bdd import given


@given('this is not a used fixture')
def not_a_fixture():
    pass
