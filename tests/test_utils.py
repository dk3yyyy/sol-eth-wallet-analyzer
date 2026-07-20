from utils import validate_wallet_address


def test_validates_real_solana_public_key_length():
    assert validate_wallet_address("11111111111111111111111111111111") == (True, "solana")
    assert validate_wallet_address("2" * 32) == (False, "invalid_solana")


def test_validates_ethereum_shape():
    assert validate_wallet_address("0x" + "a" * 40) == (True, "ethereum")
    assert validate_wallet_address("0x1234") == (False, "invalid_ethereum")
