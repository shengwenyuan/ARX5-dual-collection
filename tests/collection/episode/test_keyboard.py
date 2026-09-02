from __future__ import annotations

import os
import termios
import unittest

from arx5_collection.collection.episode.adapters.keyboard import KeyboardTrigger
from arx5_collection.collection.episode.ports import RecordTrigger, TriggerEvent


class KeyboardTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.master_fd, slave_fd = os.openpty()
        self.stream = os.fdopen(slave_fd, "r")

    def tearDown(self) -> None:
        self.stream.close()
        os.close(self.master_fd)

    def test_is_record_trigger(self) -> None:
        self.assertIsInstance(KeyboardTrigger(self.stream), RecordTrigger)

    def test_timeout_wrong_key_and_trigger_key(self) -> None:
        with KeyboardTrigger(self.stream) as trigger:
            trigger.arm()
            self.assertIsNone(trigger.wait(0.01))
            os.write(self.master_fd, b"x")
            self.assertIsNone(trigger.wait(0.1))
            os.write(self.master_fd, b" ")
            self.assertIs(trigger.wait(0.1).event, TriggerEvent.ACTIVATE)

    def test_a_is_abort_case_insensitively(self) -> None:
        with KeyboardTrigger(self.stream) as trigger:
            trigger.arm()
            os.write(self.master_fd, b"a")
            self.assertIs(trigger.wait(0.1).event, TriggerEvent.ABORT)
            os.write(self.master_fd, b"A")
            self.assertIs(trigger.wait(0.1).event, TriggerEvent.ABORT)

    def test_context_restores_terminal(self) -> None:
        original = termios.tcgetattr(self.stream.fileno())
        with KeyboardTrigger(self.stream):
            self.assertNotEqual(termios.tcgetattr(self.stream.fileno()), original)
        restored = termios.tcgetattr(self.stream.fileno())
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, original)

    def test_wait_requires_context(self) -> None:
        with self.assertRaises(RuntimeError):
            KeyboardTrigger(self.stream).wait(0.01)

    def test_arm_discards_input_received_while_disarmed(self) -> None:
        with KeyboardTrigger(self.stream) as trigger:
            os.write(self.master_fd, b" ")
            trigger.arm()
            self.assertIsNone(trigger.wait(0.01))
            os.write(self.master_fd, b" ")
            self.assertIs(trigger.wait(0.1).event, TriggerEvent.ACTIVATE)
            trigger.disarm()
            with self.assertRaisesRegex(RuntimeError, "disarmed"):
                trigger.wait(0.01)


if __name__ == "__main__":
    unittest.main()
