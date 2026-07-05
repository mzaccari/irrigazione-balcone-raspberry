"""Regressione crash-safety: lo spegnimento di sicurezza non deve sollevare
dopo che i device GPIO sono stati chiusi.

Sul Raspberry (gpiozero reale) chiamare `all_off()` su OutputDevice gia chiusi
solleva `GPIODeviceClosed`: succedeva nella rete di sicurezza `atexit` del
demone, che gira dopo `controller.close()`. Il `MockOutput` imita ora questo
comportamento (on/off dopo close() sollevano), cosi la regressione e
verificabile senza hardware.
"""

from __future__ import annotations

import pytest

from pump_controller import DEFAULT_CONFIG, MockOutput, PumpController


def test_all_off_dopo_close_non_solleva():
    controller = PumpController(DEFAULT_CONFIG, mock=True)
    controller.close()
    # Simula la rete di sicurezza atexit del demone dopo la chiusura dei device.
    controller.all_off()  # non deve sollevare


def test_mock_output_solleva_dopo_close():
    out = MockOutput(17)
    out.close()
    assert out.closed is True
    with pytest.raises(RuntimeError):
        out.off()
    with pytest.raises(RuntimeError):
        out.on()
