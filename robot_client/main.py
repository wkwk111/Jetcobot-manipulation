"""Raspberry Pi execution entry point for the laptop-local YOLO grasp service.

Keys:
  g: capture a fresh 640x480 frame + current Flange pose -> laptop plan -> validate -> grasp
  t: run the existing throw motion after a successful grasp
  w: return home while holding a grasped object
  q: request stop and exit

The laptop performs YOLO and 2D->3D grasp planning. This Pi remains responsible
for camera capture, MyCobot/gripper control, and the final local safety gate.
"""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import cv2

from . import config
from .api_client import (
    GraspServerError,
    check_server_health,
    request_grasp_plan,
)
from .robot_controller import RobotController


def _is_in_range(value: float, limits: tuple[float, float]) -> bool:
    return limits[0] <= value <= limits[1]


def validate_server_plan(
    payload: dict[str, Any],
) -> tuple[bool, str, list[float] | None]:
    """Run the final Pi-side safety validation before any robot command."""
    if payload.get("status") != "ok":
        return (
            False,
            str(payload.get("message", "target not found")),
            None,
        )

    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return False, "response.plan is missing", None

    command = plan.get("flange_command")
    if not isinstance(command, list) or len(command) != 6:
        return False, "flange_command must contain six values", None

    try:
        command = [float(v) for v in command]
    except (TypeError, ValueError):
        return False, "flange_command contains non-numeric values", None

    if not all(math.isfinite(v) for v in command):
        return False, "flange_command contains non-finite values", None

    if not _is_in_range(command[0], config.SAFE_X_MM):
        return False, f"unsafe X={command[0]:.1f} mm", None
    if not _is_in_range(command[1], config.SAFE_Y_MM):
        return False, f"unsafe Y={command[1]:.1f} mm", None
    if not _is_in_range(command[2], config.SAFE_Z_MM):
        return False, f"unsafe Z={command[2]:.1f} mm", None
    if any(abs(v) > config.SAFE_EULER_ABS_DEG for v in command[3:]):
        return False, "unsafe Euler value", None

    return True, "ok", command


def validate_frame_size(frame) -> None:
    """Reject frames that do not match the laptop's camera-calibration resolution."""
    if frame is None or getattr(frame, "ndim", 0) < 2:
        raise RuntimeError("Camera frame is invalid")

    height, width = frame.shape[:2]
    if (
        width != config.CAMERA_FRAME_WIDTH
        or height != config.CAMERA_FRAME_HEIGHT
    ):
        raise RuntimeError(
            "Camera frame size mismatch: "
            f"got {width}x{height}, expected "
            f"{config.CAMERA_FRAME_WIDTH}x{config.CAMERA_FRAME_HEIGHT}. "
            "The laptop calibration is valid only at the configured resolution."
        )


def open_calibrated_camera():
    """Open the Pi camera and make resolution mismatch fail before robot startup."""
    cap = cv2.VideoCapture(config.CAMERA_ID)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera: CAMERA_ID={config.CAMERA_ID}")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_FRAME_HEIGHT)

    # Let USB/CSI cameras apply their requested capture format before validation.
    for _ in range(max(2, config.CAMERA_FLUSH_FRAMES)):
        cap.grab()

    ret, probe_frame = cap.read()
    if not ret or probe_frame is None:
        cap.release()
        raise RuntimeError("Cannot read initial camera frame")

    try:
        validate_frame_size(probe_frame)
    except Exception:
        cap.release()
        raise

    print(
        "[CAMERA] calibrated capture size: "
        f"{config.CAMERA_FRAME_WIDTH}x{config.CAMERA_FRAME_HEIGHT}"
    )
    return cap


def capture_fresh_plan_frame(cap):
    """Capture a recent frame when G is pressed, reducing buffered-camera latency."""
    for _ in range(config.CAMERA_FLUSH_FRAMES):
        cap.grab()

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Cannot capture a current camera frame for grasp planning")

    validate_frame_size(frame)
    return frame


def draw_result(
    frame,
    payload: dict[str, Any] | None,
    error: str | None,
    throw_running: bool,
) -> None:
    text_y = 32

    cv2.putText(
        frame,
        "Laptop-local client | g: grasp | t: throw | w: home | q: quit",
        (18, text_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (0, 0, 255),
        2,
    )
    text_y += 32

    if config.DRY_RUN:
        cv2.putText(
            frame,
            "DRY RUN: robot motion disabled",
            (18, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (0, 165, 255),
            2,
        )
        text_y += 32

    if throw_running:
        cv2.putText(
            frame,
            "THROW MODE RUNNING",
            (18, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (0, 0, 255),
            2,
        )
        text_y += 32

    if payload and payload.get("status") == "ok":
        det = payload.get("detection", {})
        bbox = det.get("bbox")
        midpoint = det.get("midpoint_uv")

        if isinstance(bbox, list) and len(bbox) == 4:
            x1, box_y1, x2, box_y2 = (
                int(round(float(v))) for v in bbox
            )
            cv2.rectangle(
                frame, (x1, box_y1), (x2, box_y2), (0, 255, 0), 2
            )
            label = (
                f"{det.get('label', 'object')} "
                f"{float(det.get('confidence', 0.0)):.2f}"
            )
            cv2.putText(
                frame,
                label,
                (x1, max(24, box_y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.60,
                (0, 255, 0),
                2,
            )

        if isinstance(midpoint, list) and len(midpoint) == 2:
            u, v = (int(round(float(value))) for value in midpoint)
            cv2.drawMarker(
                frame,
                (u, v),
                (0, 0, 255),
                cv2.MARKER_CROSS,
                20,
                2,
            )

        plan = payload.get("plan", {})
        tcp = plan.get("tcp_target_base_mm")
        if isinstance(tcp, list) and len(tcp) == 3:
            text = f"TCP Base: {tcp[0]:.1f}, {tcp[1]:.1f}, {tcp[2]:.1f} mm"
            cv2.putText(
                frame,
                text,
                (18, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (0, 255, 255),
                2,
            )
            text_y += 28

        command = plan.get("flange_command")
        if isinstance(command, list) and len(command) == 6:
            text = (
                f"Flange: {command[0]:.1f}, "
                f"{command[1]:.1f}, {command[2]:.1f} mm"
            )
            cv2.putText(
                frame,
                text,
                (18, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (255, 255, 0),
                2,
            )
            text_y += 28

    if error:
        cv2.putText(
            frame,
            error[:105],
            (18, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (0, 0, 255),
            2,
        )


def main() -> None:
    print("=== Raspberry Pi -> laptop-local robot grasp client ===")
    print("Laptop endpoint:", config.GRASP_SERVER_URL)
    print("Expected runtime:", config.EXPECTED_SERVER_RUNTIME)
    print("DRY_RUN:", config.DRY_RUN)

    if config.CHECK_SERVER_ON_STARTUP:
        try:
            health = check_server_health()
            print(
                "[NETWORK] laptop server reachable: "
                f"runtime={health.get('runtime')}, "
                f"device={health.get('device')}, "
                f"model={health.get('model_path')}"
            )
        except GraspServerError as exc:
            raise RuntimeError(
                "Laptop server preflight failed. Check the [network] "
                "grasp_server_url, laptop firewall, and shared LAN path before "
                f"starting robot control.\n{exc}"
            ) from exc

    cap = open_calibrated_camera()
    robot = RobotController()

    if config.DRY_RUN:
        print("[SAFETY] DRY_RUN=True: Startup home/open is skipped.")
    else:
        robot.move_home_and_open_gripper()

    last_payload: dict[str, Any] | None = None
    last_error: str | None = None
    gripper_closed_on_target = False

    throw_running = threading.Event()
    throw_abort = threading.Event()
    throw_thread: threading.Thread | None = None

    def run_throw_worker() -> None:
        nonlocal gripper_closed_on_target, last_error
        try:
            success, message, released = robot.execute_throw_mode(
                abort_event=throw_abort
            )
            if released:
                gripper_closed_on_target = False
            if success:
                last_error = None
                print("[THROW]", message)
            else:
                last_error = f"THROW failed: {message}"
                print("[THROW]", last_error)
        except Exception as exc:
            last_error = f"THROW ERROR: {type(exc).__name__}: {exc}"
            print(last_error)
        finally:
            throw_running.clear()

    if config.SHOW_WINDOW:
        cv2.namedWindow(config.WINDOW_NAME, cv2.WINDOW_NORMAL)

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.1)
                continue

            # A runtime camera reset could change the format after startup.
            try:
                validate_frame_size(frame)
            except RuntimeError as exc:
                last_error = str(exc)
                print("[CAMERA]", last_error)
                time.sleep(0.3)
                continue

            annotated = frame.copy()
            draw_result(
                annotated,
                last_payload,
                last_error,
                throw_running.is_set(),
            )

            if config.SHOW_WINDOW:
                cv2.imshow(config.WINDOW_NAME, annotated)
                key = cv2.waitKey(1) & 0xFF
            else:
                key = 255

            if key == ord("q"):
                if throw_running.is_set():
                    print("[THROW] abort requested")
                    throw_abort.set()
                    if not config.DRY_RUN:
                        try:
                            robot.stop_motion()
                        except Exception as exc:
                            print("[THROW] stop error:", exc)
                break

            if key == ord("t"):
                if config.DRY_RUN:
                    last_error = "DRY RUN: Throw command not sent"
                elif throw_running.is_set():
                    last_error = "T ignored: throw mode is already running"
                elif not gripper_closed_on_target:
                    last_error = "T ignored: grasp an object first with G"
                else:
                    throw_abort.clear()
                    throw_running.set()
                    throw_thread = threading.Thread(
                        target=run_throw_worker,
                        daemon=False,
                    )
                    throw_thread.start()
                    last_error = None
                    print("[THROW] mode started")
                continue

            if key == ord("w"):
                if throw_running.is_set():
                    last_error = "W ignored: throw mode is running"
                elif config.DRY_RUN:
                    last_error = "DRY RUN: Home command not sent"
                elif not gripper_closed_on_target:
                    last_error = "W ignored: gripper is not closed on target"
                else:
                    reached = robot.move_home_keep_gripper_closed()
                    last_error = None if reached else "HOME return timeout"
                continue

            if key != ord("g"):
                continue

            if throw_running.is_set():
                last_error = "G ignored: throw mode is running"
                continue

            last_error = None
            try:
                # The robot is stationary here. Read its current Base-frame pose
                # and immediately capture a fresh calibration-size image.
                current_flange_coords = robot.get_flange_coords()
                plan_frame = capture_fresh_plan_frame(cap)
                payload = request_grasp_plan(plan_frame, current_flange_coords)

                print(
                    "[LAPTOP RESPONSE]\n",
                    json.dumps(payload, ensure_ascii=False, indent=2),
                )
                last_payload = payload

                is_safe, reason, command = validate_server_plan(payload)
                if not is_safe or command is None:
                    last_error = f"Plan rejected locally: {reason}"
                    print("[SAFETY]", last_error)
                    continue

                if config.DRY_RUN:
                    print(
                        "[DRY RUN] Laptop plan validated; no robot command sent:",
                        command,
                    )
                    continue

                reached = robot.send_flange_coords_and_wait(command)
                if reached:
                    robot.close_gripper()
                    gripper_closed_on_target = True
                    last_error = None
                else:
                    gripper_closed_on_target = False
                    last_error = (
                        "Target pose timeout; gripper remains unchanged"
                    )

            except (GraspServerError, RuntimeError, ValueError) as exc:
                last_error = f"ERROR: {type(exc).__name__}: {exc}"
                print(last_error)

    finally:
        if throw_running.is_set():
            throw_abort.set()
            if not config.DRY_RUN:
                try:
                    robot.stop_motion()
                except Exception:
                    pass

        if throw_thread is not None and throw_thread.is_alive():
            throw_thread.join(timeout=1.0)

        cap.release()
        if config.SHOW_WINDOW:
            cv2.destroyAllWindows()
        print("Client terminated")


if __name__ == "__main__":
    main()
