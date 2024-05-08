from pytest_bdd import given, then, when


@given("a background has already occurred")
@given("the background")
@given("a shared step")
@given("the background has a shared step")
@given("Hello")
@when("World")
@then("!")
def bckgnd():
    pass
