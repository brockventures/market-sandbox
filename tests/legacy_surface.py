"""The pre-#162 station price surface, for tests written against its exact
numbers (the #71 depot spec quotes, circuit-breaker bands around a 22 CR
Ceres FRAG, 26 CR Ceres FUEL fills). #162 narrowed every station's gap to
the four-station mean to 75% (agora/spatial.py); those tests check the
mechanics, not the surface, so they pin the surface they were written for.

    @pre_162_surface()
    def test_something(self): ...
"""
from unittest import mock

import agora.spatial as spatial

PRE_162 = {
    "earth": {"FRAG": 10.0, "FUEL": 8.0, "FOOD": 10.0, "ORE": 30.0},
    "luna": {"FRAG": 12.0, "FUEL": 16.0, "FOOD": 14.0, "ORE": 22.0},
    "mars": {"FRAG": 16.0, "FUEL": 14.0, "FOOD": 20.0, "ORE": 16.0},
    "ceres": {"FRAG": 22.0, "FUEL": 26.0, "FOOD": 30.0, "ORE": 10.0},
}


def pre_162_surface():
    """Patch agora.spatial.BASE_PRICES (the dict every module shares) to the
    pre-#162 surface for the decorated test, and restore it after."""
    return mock.patch.dict(spatial.BASE_PRICES, {st: dict(v) for st, v in PRE_162.items()})
