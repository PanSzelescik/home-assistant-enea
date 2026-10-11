"""The tariff groups the stand-in prices, by name, as enea_prices' TARIFFS."""
from ha_instance import FakeG11Tariff, FakeG12Tariff

AKCYZA = 0.005

TARIFFS = {"G11": FakeG11Tariff(), "G12": FakeG12Tariff()}
