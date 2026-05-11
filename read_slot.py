import json
import os
import subprocess
import sys
from urllib import request
from pathlib import Path


CONFIG_FILE = Path("slot_config.json")


def load_config(config_file: str | Path = CONFIG_FILE) -> dict:
    path = Path(config_file)
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def config_value(config: dict, key: str, env_name: str | None = None, default=None):
    env_key = env_name or key.upper()
    return os.environ.get(env_key) or config.get(key, default)


def _slither_read_storage_argv() -> list[str]:
    """Run Slither read-storage with the same interpreter as this script.

    On Windows, invoking the ``slither-read-storage`` console script fails with
    FileNotFoundError when ``python`` is not the venv interpreter (global Python
    is on PATH but ``venv\\Scripts`` is not). ``python -m`` uses the environment
    where Slither is installed for the running interpreter.
    """
    return [sys.executable, "-m", "slither.tools.read_storage"]


def read_storage_layout(
    contract_source: str,
    contract_name: str,
    output_file: str = "storage_layout.json",
    *,
    solc: str | Path | None = None,
):
    """Read storage layout via Slither.

    ``contract_source`` must match the Solidity ``pragma`` (e.g. ^0.6.0 needs
    solc 0.6.x). Pass ``solc`` or set env ``SOLC`` to the full path of that
    compiler binary; the ``solc`` on PATH alone is used only if it matches.
    """
    output_path = Path(output_file)
    solc_path = solc or os.environ.get("SOLC")
    solc_args: list[str] = []
    if solc_path:
        solc_args = ["--solc", str(Path(solc_path).expanduser().resolve())]

    cmd = [
        *_slither_read_storage_argv(),
        *solc_args,
        contract_source,
        "--contract-name",
        contract_name,
        "--json",
        str(output_path),
        "--silent",
    ]

    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"slither read-storage failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    with output_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def eth_get_storage_at(rpc_url: str, contract_address: str, slot: int) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getStorageAt",
        "params": [contract_address, hex(slot), "latest"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        rpc_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if "error" in result:
        raise RuntimeError(f"eth_getStorageAt failed: {result['error']}")

    return result["result"]


def decode_storage_value(layout_item: dict, raw_slot: str):
    type_string = layout_item["type_string"]
    size_bits = int(layout_item["size"])
    offset_bits = int(layout_item["offset"])
    slot_value = int(raw_slot, 16)
    mask = (1 << size_bits) - 1
    packed_value = (slot_value >> offset_bits) & mask

    if type_string == "bool":
        return packed_value != 0
    if type_string.startswith("uint"):
        return packed_value
    if type_string == "address":
        return "0x" + packed_value.to_bytes(20, byteorder="big").hex()

    return packed_value


def read_variable_value(layout: dict, variable_name: str, rpc_url: str, contract_address: str):
    if variable_name not in layout:
        raise KeyError(f"variable not found in layout: {variable_name}")

    layout_item = layout[variable_name]
    raw_slot = eth_get_storage_at(rpc_url, contract_address, int(layout_item["slot"]))
    decoded_value = decode_storage_value(layout_item, raw_slot)

    return {
        "variable": variable_name,
        "type": layout_item["type_string"],
        "slot": layout_item["slot"],
        "offset": layout_item["offset"],
        "raw_slot": raw_slot,
        "value": decoded_value,
    }


if __name__ == "__main__":
    config = load_config()

    # Prefer explicit SOLC / PATH solc; otherwise use py-solc-x if installed (pip install py-solc-x).
    solc_bin = config_value(config, "solc", "SOLC")
    solc_version = config_value(config, "solc_version", "SOLC_VERSION", "0.8.2")
    if not solc_bin:
        try:
            import solcx

            solcx.install_solc(solc_version)
            solc_bin = solcx.install.get_executable(solc_version)
        except ImportError:
            pass

    layout = read_storage_layout(
        contract_source=config_value(config, "contract_source", "CONTRACT_SOURCE", "./contracts/token.sol"),
        contract_name=config_value(config, "contract_name", "CONTRACT_NAME", "AnyswapV5ERC20"),
        output_file=config_value(config, "output_file", "OUTPUT_FILE", "storage_layout.json"),
        solc=solc_bin,
    )

    print(json.dumps(layout, indent=2, ensure_ascii=False))

    rpc_url = config_value(config, "rpc_url", "RPC_URL")
    contract_address = config_value(config, "contract_address", "CONTRACT_ADDRESS")
    variable_name = config_value(config, "variable_name", "VARIABLE_NAME", "_init")

    if rpc_url and contract_address:
        try:
            value = read_variable_value(
                layout=layout,
                variable_name=variable_name,
                rpc_url=rpc_url,
                contract_address=contract_address,
            )
            print(json.dumps(value, indent=2, ensure_ascii=False))
        except Exception as exc:
            print(
                "\nFailed to read on-chain storage value. "
                "Please check rpc_url, contract_address, and network connectivity."
            )
            raise
    else:
        print(
            "\nSet RPC_URL and CONTRACT_ADDRESS to read an on-chain value, "
            "for example VARIABLE_NAME=_init."
        )
