bl_info = {
    "name": "FrillCreate",
    "author": "kura",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "3D View",
    "description": "Show a Hello popup with a complex shortcut",
    "category": "3D View",
}

import json
import math
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup
from mathutils import Vector


PIN_GROUP = "Frill_Gather_Pin"
TIP_GROUP = "Frill_Tip"
ALL_GROUP = "Frill_All"
WORLD_UP = Vector((0.0, 0.0, 1.0))
HEIGHT_RESPONSE_SCALE = 0.1
MAX_UPDATE_DELAY_SECONDS = 0.5
LINE_CONTROL_POINT_DEFINITIONS = (
    ("positive_in", "Positive In"),
    ("positive_peak", "Positive Peak"),
    ("positive_out", "Positive Out"),
    ("negative_in", "Negative In"),
    ("negative_trough", "Negative Trough"),
    ("negative_out", "Negative Out"),
)
LINE_CONTROL_UI_ORDER = LINE_CONTROL_POINT_DEFINITIONS
POINT_RANDOM_INDICES = {
    point_key: index
    for index, (point_key, _label) in enumerate(LINE_CONTROL_UI_ORDER, start=1)
}
POINT_LENGTH_SIGNS = {
    "negative_in": -1.0,
    "negative_trough": 1.0,
    "negative_out": 1.0,
    "positive_in": -1.0,
    "positive_peak": 1.0,
    "positive_out": 1.0,
}
POINT_DEPTH_SIGNS = {
    "negative_in": 1.0,
    "negative_trough": 1.0,
    "negative_out": 1.0,
    "positive_in": -1.0,
    "positive_peak": -1.0,
    "positive_out": -1.0,
}
GATHER_POINT_DEPTH_SIGNS = {
    "negative_in": 1.0,
    "negative_trough": 1.0,
    "negative_out": 1.0,
    "positive_in": -1.0,
    "positive_peak": -1.0,
    "positive_out": -1.0,
}
POINT_ROTATION_SIGNS = {
    "negative_in": 1.0,
    "negative_trough": 1.0,
    "negative_out": -1.0,
    "positive_in": 1.0,
    "positive_peak": 1.0,
    "positive_out": -1.0,
}
EDIT_MODE_IDENTIFIER = "__EDIT_MODE__"
EDIT_MODE_ITEM = (EDIT_MODE_IDENTIFIER, "※EditMode", "Use directly editable curve control points")
ANGLE_POINT_MAX = 1000
CURVE_DEFORM_AXIS_ITEMS = (
    ('POS_X', "X", "Deform the frill mesh along the positive X axis"),
    ('NEG_X', "-X", "Deform the frill mesh along the negative X axis"),
    ('POS_Y', "Y", "Deform the frill mesh along the positive Y axis"),
    ('NEG_Y', "-Y", "Deform the frill mesh along the negative Y axis"),
    ('POS_Z', "Z", "Deform the frill mesh along the positive Z axis"),
    ('NEG_Z', "-Z", "Deform the frill mesh along the negative Z axis"),
)
SOURCE_CURVE_HANDLE_TYPE_ITEMS = (
    ('ALIGNED', "Aligned", "Use aligned Bezier handles for the Source curve"),
    ('VECTOR', "Vector", "Use vector handles so the Source curve follows straight segments between points"),
)
CURVE_MODIFIER_NAME = "Frill Curve Deform"


def selected_edge_world_points(context):
    obj = context.object
    if obj is None or obj.type != 'MESH':
        raise ValueError("Select a mesh object.")

    previous_mode = obj.mode
    if previous_mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')

    mesh = obj.data
    selected_edges = [edge for edge in mesh.edges if edge.select]
    if not selected_edges:
        if previous_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        raise ValueError("Select a continuous edge chain.")

    vertex_ids = {vid for edge in selected_edges for vid in edge.vertices}
    adjacency = defaultdict(list)
    edge_keys = set()
    for edge in selected_edges:
        a, b = edge.vertices
        adjacency[a].append(b)
        adjacency[b].append(a)
        edge_keys.add(tuple(sorted((a, b))))

    if any(len(linked) > 2 for linked in adjacency.values()):
        raise ValueError("Branched edge selections are not supported in v0.2.")

    visited = set()
    queue = deque([next(iter(vertex_ids))])
    while queue:
        vid = queue.popleft()
        if vid in visited:
            continue
        visited.add(vid)
        queue.extend(adjacency[vid])

    if visited != vertex_ids:
        raise ValueError("Disconnected edge selections are not supported.")

    degree_one = [vid for vid in vertex_ids if len(adjacency[vid]) == 1]
    is_loop = len(degree_one) == 0
    if not is_loop and len(degree_one) != 2:
        raise ValueError("Could not identify the edge-chain endpoints.")

    start = next(iter(vertex_ids)) if is_loop else degree_one[0]
    ordered_ids = [start]
    previous = None
    current = start
    used_edges = set()

    while True:
        next_candidates = []
        for linked in adjacency[current]:
            key = tuple(sorted((current, linked)))
            if key not in used_edges and linked != previous:
                next_candidates.append(linked)

        if not next_candidates:
            if is_loop:
                closing = tuple(sorted((current, start)))
                if closing in edge_keys and closing not in used_edges:
                    used_edges.add(closing)
                    ordered_ids.append(start)
            break

        next_id = next_candidates[0]
        used_edges.add(tuple(sorted((current, next_id))))
        ordered_ids.append(next_id)
        previous, current = current, next_id

        if is_loop and current == start:
            break

    if len(used_edges) != len(edge_keys):
        raise ValueError("Failed to order the selected edge chain.")

    matrix = obj.matrix_world
    points = [matrix @ mesh.vertices[vid].co for vid in ordered_ids]
    source_edges = [vid for edge in sorted(edge_keys) for vid in edge]
    return points, is_loop, obj, ordered_ids, source_edges


def cumulative_lengths(points):
    lengths = [0.0]
    total = 0.0
    for index in range(1, len(points)):
        total += (points[index] - points[index - 1]).length
        lengths.append(total)
    return lengths, total


def point_at_distance(points, lengths, total, distance, is_loop):
    if is_loop:
        distance = distance % total
    elif distance <= 0.0:
        tangent = (points[1] - points[0]).normalized()
        return points[0] + tangent * distance, tangent
    elif distance >= total:
        tangent = (points[-1] - points[-2]).normalized()
        return points[-1] + tangent * (distance - total), tangent

    segment_index = 1
    while segment_index < len(lengths) - 1 and lengths[segment_index] < distance:
        segment_index += 1

    start = points[segment_index - 1]
    end = points[segment_index]
    segment_length = max(0.000001, lengths[segment_index] - lengths[segment_index - 1])
    factor = (distance - lengths[segment_index - 1]) / segment_length
    return start.lerp(end, factor), (end - start).normalized()


def curve_resolution_u(settings):
    return max(1, settings.resolution_u)


def curve_handle_scale(settings):
    return 0.4075


def is_edit_mode(settings):
    return settings.frill_type == EDIT_MODE_IDENTIFIER


def edit_curve_point_count(settings, source_point_count, is_loop):
    max_count = max(2, source_point_count - 1 if is_loop and source_point_count > 2 else source_point_count)
    return max(2, min(max_count, settings.edit_curve_point_count))


def edit_mode_control_entries(settings, is_loop, source_point_count):
    point_count = edit_curve_point_count(settings, source_point_count, is_loop)
    entries = []
    if is_loop:
        for index in range(point_count):
            entries.append({
                "u": index / point_count,
                "section": 'EDIT',
                "t": 0.0,
                "point_key": None,
                "endpoint": True,
            })
    else:
        denominator = max(1, point_count - 1)
        for index in range(point_count):
            entries.append({
                "u": index / denominator,
                "section": 'EDIT',
                "t": 0.0,
                "point_key": None,
                "endpoint": True,
            })
    return entries


def curve_control_entries(settings, is_loop, source_point_count=None):
    if is_edit_mode(settings):
        return edit_mode_control_entries(settings, is_loop, source_point_count or 2)

    wave_count = max(1, settings.tip_wave_count)
    entries = []

    def add_entry(u_value, section, section_t, point_key, endpoint=False, wave_index=None):
        if is_loop:
            u_value = u_value % 1.0
        else:
            u_value = max(0.0, min(1.0, u_value))
        entries.append({
            "u": u_value,
            "section": section,
            "t": max(0.0, min(1.0, section_t)),
            "point_key": point_key,
            "endpoint": endpoint,
            "wave_index": wave_index,
        })

    if not is_loop:
        add_entry(0.0, 'ENDPOINT', 0.0, None, endpoint=True)

    for wave_index in range(wave_count):
        start = wave_index / wave_count
        cycle = 1.0 / wave_count
        add_entry(start + cycle * (1.0 / 12.0), 'NEGATIVE', 1.0 / 6.0, "negative_in", wave_index=wave_index)
        add_entry(start + cycle * (3.0 / 12.0), 'NEGATIVE', 0.5, "negative_trough", wave_index=wave_index)
        add_entry(start + cycle * (5.0 / 12.0), 'NEGATIVE', 5.0 / 6.0, "negative_out", wave_index=wave_index)
        add_entry(start + cycle * (7.0 / 12.0), 'POSITIVE', 1.0 / 6.0, "positive_in", wave_index=wave_index)
        add_entry(start + cycle * (9.0 / 12.0), 'POSITIVE', 0.5, "positive_peak", wave_index=wave_index)
        add_entry(start + cycle * (11.0 / 12.0), 'POSITIVE', 5.0 / 6.0, "positive_out", wave_index=wave_index)

    if not is_loop:
        add_entry(1.0, 'ENDPOINT', 1.0, None, endpoint=True)

    if is_loop:
        unique = {}
        for entry in entries:
            key = round(entry["u"], 6)
            unique[key] = entry
        entries = list(unique.values())

    entries.sort(key=lambda item: item["u"])
    return entries


def sample_polyline_control_entries(points, entries, is_loop, length_ratio=1.0):
    if is_loop and points[0] != points[-1]:
        points = points + [points[0]]

    lengths, total = cumulative_lengths(points)
    if total <= 0.000001:
        raise ValueError("The selected edge chain is too short.")

    samples = []
    for entry in entries:
        distance = total * entry["u"] * length_ratio
        position, tangent = point_at_distance(points, lengths, total, distance, is_loop)
        outward = tangent.cross(WORLD_UP)
        if outward.length < 0.0001:
            outward = tangent.cross(Vector((0.0, 1.0, 0.0)))
        outward.normalize()
        samples.append((position, tangent, outward, entry["u"], entry))

    return samples


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def smooth_noise_values(count, seed, randomness):
    rng = random.Random(seed)
    anchors = max(4, min(16, count // 4 + 2))
    values = [rng.uniform(-1.0, 1.0) for _ in range(anchors + 1)]
    result = []

    for index in range(count):
        t = index / max(1, count - 1)
        scaled = t * (anchors - 1)
        base = int(math.floor(scaled))
        frac = smoothstep(scaled - base)
        value = values[base] * (1.0 - frac) + values[base + 1] * frac
        result.append(value * randomness)

    return result


def frill_height_value(settings):
    return max(0.001, settings.width * HEIGHT_RESPONSE_SCALE)


def base_outward_for_tilt(base_outward, settings):
    outward = base_outward.copy()
    if outward.length < 0.0001:
        outward = Vector((1.0, 0.0, 0.0))

    outward.normalize()
    return outward


def angle_point_value(settings, index):
    if index == 0:
        return settings.direction_angle
    return getattr(settings, f"angle_point_{index + 1:04d}", 0.0)


def angle_at_u(settings, u_value):
    count = max(1, min(ANGLE_POINT_MAX, settings.angle_point_count))
    if count == 1:
        return angle_point_value(settings, 0)

    u_value = max(0.0, min(1.0, u_value))
    first_center = 0.5 / count
    last_center = (count - 0.5) / count
    if u_value <= first_center:
        return angle_point_value(settings, 0)
    if u_value >= last_center:
        return angle_point_value(settings, count - 1)

    scaled = u_value * count - 0.5
    lower = max(0, min(count - 2, int(math.floor(scaled))))
    upper = lower + 1
    factor = smoothstep(scaled - lower)
    lower_angle = angle_point_value(settings, lower)
    upper_angle = angle_point_value(settings, upper)
    return lower_angle * (1.0 - factor) + upper_angle * factor


def source_curve_tilt_values(points, settings, is_loop):
    count = len(points)
    if count == 0:
        return []
    tilts = []
    for index in range(count):
        if settings.angle_direct_control:
            angle = angle_point_value(settings, min(index, ANGLE_POINT_MAX - 1))
        else:
            u_value = index / count if is_loop else index / max(1, count - 1)
            angle = angle_at_u(settings, u_value)
        tilts.append(math.radians(angle))
    return tilts


def tilted_frame(tangent, base_outward, settings, u_value=0.0):
    angle = 0.0
    tangent = tangent.normalized()
    base_outward = base_outward_for_tilt(base_outward, settings)

    up = tangent.cross(base_outward)
    if up.length < 0.0001:
        up = WORLD_UP.copy()
    up.normalize()

    outward = base_outward * math.cos(angle) + up * math.sin(angle)
    up = -base_outward * math.sin(angle) + up * math.cos(angle)
    outward.normalize()
    up.normalize()

    lateral = tangent.copy()
    if lateral.length < 0.0001:
        lateral = up.cross(outward)
    lateral.normalize()
    return outward, up, lateral


def vertical_wave_for_entry(entry, settings):
    return 0.0, 0.0, 0.0, 0.0


def cycle_point_key_for_settings(point_key, settings):
    if point_key == "negative_out" and settings.cycle_pair_negative_in_out:
        return "negative_in"
    if point_key == "positive_out" and settings.cycle_pair_positive_in_out:
        return "positive_in"
    return point_key


def random_index_for_line_point(source_key, wave_index):
    point_index = POINT_RANDOM_INDICES.get(source_key, 0)
    if wave_index is None:
        return point_index
    return int(wave_index) * max(1, len(POINT_RANDOM_INDICES)) + point_index


def cycle_point_control(settings, point_key, wave_index=None):
    if point_key is None:
        return 0.0, 0.0, 0.0, 1.0, 0.0

    source_key = cycle_point_key_for_settings(point_key, settings)
    random_index = random_index_for_line_point(source_key, wave_index)
    move_scale = frill_height_value(settings)
    random_move_x, random_move_y, random_move_z, random_handle_length, random_handle_rotation = point_random_offsets(
        source_key,
        random_index,
        settings.cycle_random_enabled,
        settings.cycle_random_seed,
        settings.cycle_random_move_offset,
        settings.cycle_random_handle_offset,
        settings.cycle_random_rotation_offset,
        17011,
    )
    move_x = (getattr(settings, f"{source_key}_move_x") + random_move_x) * move_scale
    move_y = (getattr(settings, f"{source_key}_move_y") + random_move_y) * move_scale
    move_z = (getattr(settings, f"{source_key}_move_z") + random_move_z + settings.cycle_all_depth_offset) * move_scale
    handle_length = max(0.0, getattr(settings, f"{source_key}_handle_length") + random_handle_length)
    handle_rotation = getattr(settings, f"{source_key}_handle_rotation") + random_handle_rotation

    move_x *= POINT_LENGTH_SIGNS.get(point_key, 1.0)
    move_z *= POINT_DEPTH_SIGNS.get(point_key, 1.0)
    handle_rotation *= POINT_ROTATION_SIGNS.get(point_key, 1.0)

    return move_x, move_y, move_z, handle_length, handle_rotation


def gather_point_key_for_settings(point_key, settings):
    if point_key == "negative_out" and settings.gather_pair_negative_in_out:
        return "negative_in"
    if point_key == "positive_out" and settings.gather_pair_positive_in_out:
        return "positive_in"
    return point_key


def point_random_offsets(point_key, random_index, enabled, seed, move_offset, handle_offset, rotation_offset, seed_salt):
    if not enabled or point_key is None:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    point_index = POINT_RANDOM_INDICES.get(point_key, 0)
    rng = random.Random(int(seed) * 1009 + seed_salt + point_index * 7919 + int(random_index) * 104729)
    move_offset = max(0.0, move_offset)
    handle_offset = max(0.0, handle_offset)
    rotation_offset = max(0.0, rotation_offset)
    return (
        rng.uniform(-move_offset, move_offset),
        rng.uniform(-move_offset, move_offset),
        rng.uniform(-move_offset, move_offset),
        rng.uniform(-handle_offset, handle_offset),
        rng.uniform(-rotation_offset, rotation_offset),
    )


def gather_cycle_point_control(settings, point_key, wave_index=None):
    if point_key is None:
        return 0.0, 0.0, 0.0, 1.0, 0.0

    source_key = gather_point_key_for_settings(point_key, settings)
    random_index = random_index_for_line_point(source_key, wave_index)
    move_scale = frill_height_value(settings)
    random_move_x, random_move_y, random_move_z, random_handle_length, random_handle_rotation = point_random_offsets(
        source_key,
        random_index,
        settings.gather_random_enabled,
        settings.gather_random_seed,
        settings.gather_random_move_offset,
        settings.gather_random_handle_offset,
        settings.gather_random_rotation_offset,
        43019,
    )
    move_x = (getattr(settings, f"gather_{source_key}_move_x") + random_move_x) * move_scale
    move_y = (getattr(settings, f"gather_{source_key}_move_y") + random_move_y) * move_scale
    move_z = (getattr(settings, f"gather_{source_key}_move_z") + random_move_z + settings.gather_all_depth_offset) * move_scale
    handle_length = max(0.0, getattr(settings, f"gather_{source_key}_handle_length") + random_handle_length)
    handle_rotation = getattr(settings, f"gather_{source_key}_handle_rotation") + random_handle_rotation

    move_x *= POINT_LENGTH_SIGNS.get(point_key, 1.0)
    move_z *= GATHER_POINT_DEPTH_SIGNS.get(point_key, 1.0)
    handle_rotation *= POINT_ROTATION_SIGNS.get(point_key, 1.0)

    return move_x, move_y, move_z, handle_length, handle_rotation


def build_curve_control_frill_shape(points, is_loop, settings):
    entries = curve_control_entries(settings, is_loop, len(points))
    samples = sample_polyline_control_entries(points, entries, is_loop, settings.length_ratio)
    count = len(samples)
    lateral_frequency = max(0.01, settings.tip_lateral_frequency)
    width_noise = smooth_noise_values(count, settings.seed, settings.randomness)
    lateral_noise = smooth_noise_values(count, settings.seed + 197, settings.tip_lateral_randomness)
    step_lengths = [
        (samples[index][0] - samples[index - 1][0]).length
        for index in range(1, count)
    ]
    average_step = sum(step_lengths) / max(1, len(step_lengths))

    columns = []
    for index, (base, tangent, base_outward, u_value, entry) in enumerate(samples):
        outward, up, lateral = tilted_frame(tangent, base_outward, settings, u_value)
        vertical_wave, vertical_depth, tangent_shift, height_shift = vertical_wave_for_entry(entry, settings)
        vertical_offset = vertical_depth * vertical_wave

        phase = math.radians(settings.tip_lateral_phase)
        lateral_wave = math.sin(u_value * math.tau * lateral_frequency + phase + lateral_noise[index])
        lateral_offset = settings.tip_lateral_amplitude * lateral_wave

        attach_offset = 0.0
        gather_move_x = gather_move_y = gather_move_z = 0.0
        gather_handle_length = 1.0
        gather_handle_rotation = 0.0
        wave_index = entry.get("wave_index")
        gather_move_x, gather_move_y, gather_move_z, gather_handle_length, gather_handle_rotation = gather_cycle_point_control(settings, entry["point_key"], wave_index)

        point_key = entry["point_key"]
        move_x, move_y, move_z, handle_length, handle_rotation = cycle_point_control(settings, point_key, wave_index)
        height_value = frill_height_value(settings)
        random_width = height_value * width_noise[index] * 0.25
        attach = (
            base
            + outward * attach_offset
            + tangent.normalized() * gather_move_x
            + outward * gather_move_y
            + up * gather_move_z
        )
        width = max(0.001, height_value + random_width)
        columns.append({
            "source": base,
            "attach": attach,
            "u": u_value,
            "outward": outward,
            "up": up,
            "lateral": lateral,
            "width": width,
            "vertical_offset": vertical_offset,
            "lateral_offset": lateral_offset,
            "tangent": tangent.normalized(),
            "tangent_shift": tangent_shift * average_step,
            "height_shift": height_shift,
            "point_move_length": move_x,
            "point_move_height": move_y,
            "point_move_depth": move_z,
            "attach_move_length": gather_move_x,
            "attach_move_height": gather_move_y,
            "attach_move_depth": gather_move_z,
            "handle_length": handle_length,
            "handle_rotation": handle_rotation,
            "handle_axis": outward.copy(),
            "attach_handle_length": gather_handle_length,
            "attach_handle_rotation": gather_handle_rotation,
            "attach_handle_axis": outward.copy(),
        })

    return columns


def bezier_point(p0, h0, h1, p1, t):
    one_minus_t = 1.0 - t
    return (
        p0 * (one_minus_t ** 3)
        + h0 * (3.0 * one_minus_t * one_minus_t * t)
        + h1 * (3.0 * one_minus_t * t * t)
        + p1 * (t ** 3)
    )


def rotate_vector_around_axis(vector, axis, angle):
    if axis is None or axis.length < 0.000001 or abs(angle) < 0.000001:
        return vector

    axis = axis.normalized()
    return (
        vector * math.cos(angle)
        + axis.cross(vector) * math.sin(angle)
        + axis * axis.dot(vector) * (1.0 - math.cos(angle))
    )


def set_bezier_handles(spline, handle_scale=0.42, handle_controls=None, handle_type='ALIGNED'):
    points = spline.bezier_points
    count = len(points)
    if count < 2:
        return

    handle_type = handle_type if handle_type in {'ALIGNED', 'VECTOR'} else 'ALIGNED'
    for index, point in enumerate(points):
        point.handle_left_type = 'ALIGNED'
        point.handle_right_type = 'ALIGNED'
        control = handle_controls[index] if handle_controls and index < len(handle_controls) else {}
        length_multiplier = max(0.0, control.get("length", 1.0))
        rotation = math.radians(control.get("rotation", 0.0))
        axis = control.get("axis")

        if spline.use_cyclic_u:
            previous_point = points[(index - 1) % count]
            next_point = points[(index + 1) % count]
            direction = next_point.co - previous_point.co
            left_length = (point.co - previous_point.co).length * handle_scale
            right_length = (next_point.co - point.co).length * handle_scale
        elif index == 0:
            next_point = points[index + 1]
            direction = next_point.co - point.co
            left_length = 0.0
            right_length = direction.length * handle_scale
        elif index == count - 1:
            previous_point = points[index - 1]
            direction = point.co - previous_point.co
            left_length = direction.length * handle_scale
            right_length = 0.0
        else:
            previous_point = points[index - 1]
            next_point = points[index + 1]
            direction = next_point.co - previous_point.co
            left_length = (point.co - previous_point.co).length * handle_scale
            right_length = (next_point.co - point.co).length * handle_scale

        if direction.length < 0.000001:
            direction = Vector((1.0, 0.0, 0.0))
        direction.normalize()
        direction = rotate_vector_around_axis(direction, axis, rotation)
        if direction.length < 0.000001:
            direction = Vector((1.0, 0.0, 0.0))
        direction.normalize()

        if spline.use_cyclic_u or (index != 0 and index != count - 1):
            equal_length = (left_length + right_length) * 0.5 * length_multiplier
            left_length = equal_length
            right_length = equal_length
        else:
            left_length *= length_multiplier
            right_length *= length_multiplier

        point.handle_left = point.co - direction * left_length
        point.handle_right = point.co + direction * right_length
        point.handle_left_type = handle_type
        point.handle_right_type = handle_type


def create_bezier_curve_object(name, points, is_loop, collection, resolution_u=12, handle_scale=0.42, handle_controls=None, tilt_values=None, handle_type='ALIGNED'):
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.resolution_u = max(1, resolution_u)
    curve.bevel_depth = 0.0
    curve.use_path = False
    spline = curve.splines.new('BEZIER')
    spline.bezier_points.add(max(0, len(points) - 1))
    spline.use_cyclic_u = is_loop

    for point, co in zip(spline.bezier_points, points):
        point.co = co
    if tilt_values:
        for point, tilt in zip(spline.bezier_points, tilt_values):
            point.tilt = tilt
    set_bezier_handles(spline, handle_scale, handle_controls, handle_type)

    obj = bpy.data.objects.new(name, curve)
    collection.objects.link(obj)
    return obj


def create_generated_frill_collection(context, source_obj):
    parent_collection = context.collection or context.scene.collection
    generated_collection = bpy.data.collections.new(f"{source_obj.name}_Frill_Generated")
    parent_collection.children.link(generated_collection)
    return parent_collection, generated_collection


def generated_frill_collection(frill_obj, fallback_collection):
    collection_name = frill_obj.get("frill_generated_collection", "")
    return bpy.data.collections.get(collection_name) or fallback_collection


def set_bezier_curve_points(curve_obj, points, is_loop, resolution_u=None, handle_scale=0.42, handle_controls=None, tilt_values=None, handle_type='ALIGNED'):
    if curve_obj is None or curve_obj.type != 'CURVE':
        raise ValueError("Frill curve source is missing.")

    curve = curve_obj.data
    if resolution_u is not None:
        curve.resolution_u = max(1, resolution_u)
    curve.splines.clear()
    spline = curve.splines.new('BEZIER')
    spline.bezier_points.add(max(0, len(points) - 1))
    spline.use_cyclic_u = is_loop

    inv_matrix = curve_obj.matrix_world.inverted()
    for point, world_co in zip(spline.bezier_points, points):
        point.co = inv_matrix @ world_co
    if tilt_values:
        for point, tilt in zip(spline.bezier_points, tilt_values):
            point.tilt = tilt
    set_bezier_handles(spline, handle_scale, handle_controls, handle_type)


def sample_bezier_curve_object(curve_obj, sample_count):
    if curve_obj is None or curve_obj.type != 'CURVE' or not curve_obj.data.splines:
        raise ValueError("Frill curve source is missing.")

    spline = curve_obj.data.splines[0]
    if spline.type != 'BEZIER' or len(spline.bezier_points) < 2:
        raise ValueError("Frill curve source must be a Bezier curve with at least two points.")

    points = spline.bezier_points
    segment_count = len(points) if spline.use_cyclic_u else len(points) - 1
    sample_count = max(2, sample_count)
    sampled = []

    for sample_index in range(sample_count):
        if spline.use_cyclic_u:
            u = sample_index / sample_count
        else:
            u = sample_index / max(1, sample_count - 1)

        scaled = u * segment_count
        segment_index = min(segment_count - 1, int(math.floor(scaled)))
        t = scaled - segment_index
        p0 = points[segment_index]
        p1 = points[(segment_index + 1) % len(points)]
        co = bezier_point(p0.co, p0.handle_right, p1.handle_left, p1.co, t)
        sampled.append(curve_obj.matrix_world @ co)

    return sampled


def curve_control_points_from_object(curve_obj):
    if curve_obj is None or curve_obj.type != 'CURVE' or not curve_obj.data.splines:
        raise ValueError("Frill curve source is missing.")

    spline = curve_obj.data.splines[0]
    if spline.type != 'BEZIER' or len(spline.bezier_points) < 2:
        raise ValueError("Frill curve source must be a Bezier curve with at least two points.")

    return [curve_obj.matrix_world @ point.co for point in spline.bezier_points]


def curve_sample_count_from_resolution(curve_obj):
    if curve_obj is None or curve_obj.type != 'CURVE' or not curve_obj.data.splines:
        return 2

    spline = curve_obj.data.splines[0]
    if spline.type != 'BEZIER' or len(spline.bezier_points) < 2:
        return 2

    segment_count = len(spline.bezier_points) if spline.use_cyclic_u else len(spline.bezier_points) - 1
    resolution = max(1, int(curve_obj.data.resolution_u))
    endpoint = 0 if spline.use_cyclic_u else 1
    return max(2, segment_count * resolution + endpoint)


def profile_blend_for_u(u_value, wave_count):
    phase = (u_value * max(1, wave_count)) % 1.0
    transition = 0.08

    # Keep each half-cycle independent, and only blend near the borders where
    # the active mid-shape changes from negative to positive or back again.
    if phase < transition:
        blend = smoothstep(phase / transition)
        return 0.5 * (1.0 - blend)
    if phase < 0.5 - transition:
        return 0.0
    if phase < 0.5 + transition:
        blend = smoothstep((phase - (0.5 - transition)) / (transition * 2.0))
        return blend
    if phase < 1.0 - transition:
        return 1.0

    blend = smoothstep((phase - (1.0 - transition)) / transition)
    return 1.0 - 0.5 * blend


def mesh_depth_axis_for_column(attach_points, tip_points, index, is_loop):
    count = min(len(attach_points), len(tip_points))
    previous_index = (index - 1) % count if is_loop else max(0, index - 1)
    next_index = (index + 1) % count if is_loop else min(count - 1, index + 1)
    previous_center = attach_points[previous_index].lerp(tip_points[previous_index], 0.5)
    next_center = attach_points[next_index].lerp(tip_points[next_index], 0.5)
    tangent = next_center - previous_center
    span = tip_points[index] - attach_points[index]
    if tangent.length < 0.000001 or span.length < 0.000001:
        return WORLD_UP.copy()

    depth_axis = tangent.normalized().cross(span.normalized())
    if depth_axis.length < 0.000001:
        return WORLD_UP.copy()
    return depth_axis.normalized()


def mid_profile_power_exponent(control_value):
    control_value = max(0.0, min(1.0, control_value))
    return 8.0 - control_value * 7.95


def mid_profile_offset(v_value, depth, power, center, span_length):
    v_value = max(0.0, min(1.0, v_value))
    if abs(depth) < 0.000001 or v_value <= 0.0 or v_value >= 1.0:
        return 0.0
    center = max(0.001, min(0.999, center))
    if v_value <= center:
        profile = math.sin((v_value / center) * math.pi * 0.5)
    else:
        profile = math.sin(((1.0 - v_value) / (1.0 - center)) * math.pi * 0.5)
    profile = max(0.0, profile) ** mid_profile_power_exponent(power)
    return depth * span_length * profile


def source_curve_points(points):
    if len(points) > 2 and (points[0] - points[-1]).length < 0.000001:
        return points[:-1]
    return points


def source_curve_points_for_settings(points, settings):
    source_points = source_curve_points(points)
    if settings.reverse_source_curve:
        return list(reversed(source_points))
    return source_points


def source_curve_length(points, length_ratio=1.0):
    if len(points) < 2:
        return 0.0
    working_points = points
    lengths, total = cumulative_lengths(working_points)
    return max(0.001, total * max(0.01, length_ratio))


def strip_curve_points_from_columns(columns, source_length, is_loop, settings):
    curve_columns = columns
    if len(curve_columns) < 2:
        raise ValueError("Frill columns did not produce enough points.")

    attach_points = []
    tip_points = []
    for u_index, column in enumerate(curve_columns):
        u_value = column.get("u", u_index / len(curve_columns) if is_loop else u_index / max(1, len(curve_columns) - 1))
        x = source_length * u_value
        attach_x = x + column.get("attach_move_length", 0.0)
        attach_y = column.get("attach_move_depth", 0.0)
        attach_z = column.get("attach_move_height", 0.0)
        if settings.gather_smoothing_enabled:
            attach_z += settings.gather_shape_offset * frill_height_value(settings)
        tip_x = (
            x
            + column.get("lateral_offset", 0.0)
            + column.get("tangent_shift", 0.0)
            + column.get("point_move_length", 0.0)
        )
        tip_y = column.get("vertical_offset", 0.0) + column.get("point_move_depth", 0.0)
        tip_z = (
            column.get("width", settings.width)
            + column.get("height_shift", 0.0)
            + column.get("point_move_height", 0.0)
        )
        if settings.turn_mesh_x:
            attach_x = -attach_x
            tip_x = -tip_x
        attach_points.append(Vector((attach_x, attach_y, attach_z)))
        tip_points.append(Vector((tip_x, tip_y, tip_z)))
    return attach_points, tip_points


def strip_curve_handle_controls_from_columns(columns, settings):
    axis = Vector((0.0, 0.0, 1.0))
    attach_controls = []
    tip_controls = []
    for column in columns:
        attach_controls.append({
            "length": column.get("attach_handle_length", 1.0),
            "rotation": column.get("attach_handle_rotation", 0.0),
            "axis": axis,
        })
        tip_controls.append({
            "length": column.get("handle_length", 1.0),
            "rotation": column.get("handle_rotation", 0.0),
            "axis": axis,
        })
    return attach_controls, tip_controls


def build_curve_deformed_mesh_data_from_points(attach_points, tip_points, is_loop, settings):
    count = min(len(attach_points), len(tip_points))
    if count < 2:
        raise ValueError("Frill strip curves did not produce enough points.")

    resolution_v = max(1, settings.resolution_v)
    verts = []
    uvs = []
    use_source_row = bool(getattr(settings, "gather_smoothing_enabled", False))
    for u_index in range(count):
        attach = attach_points[u_index]
        tip = tip_points[u_index]
        u_value = u_index / count if is_loop else u_index / max(1, count - 1)
        span = tip - attach
        span_length = max(0.001, span.length)
        positive_blend = profile_blend_for_u(u_value, settings.tip_wave_count)
        negative_blend = 1.0 - positive_blend
        if use_source_row:
            verts.append((attach.x, 0.0, 0.0))
            uvs.append((u_value, 0.0))
        for v_index in range(resolution_v + 1):
            v_value = v_index / resolution_v
            positive_mid_offset = mid_profile_offset(
                v_value,
                settings.positive_mid_depth,
                settings.positive_mid_profile_power,
                settings.positive_mid_center,
                span_length,
            )
            negative_mid_offset = mid_profile_offset(
                v_value,
                settings.negative_mid_depth,
                settings.negative_mid_profile_power,
                settings.negative_mid_center,
                span_length,
            )
            mid_offset = positive_mid_offset * positive_blend + negative_mid_offset * negative_blend
            position = attach.lerp(tip, v_value) + Vector((0.0, mid_offset, 0.0))
            verts.append(tuple(position))
            uv_v = (v_index + 1) / (resolution_v + 1) if use_source_row else v_value
            uvs.append((u_value, uv_v))

    faces = []
    row = resolution_v + 2 if use_source_row else resolution_v + 1
    u_segments = count if is_loop else count - 1
    for u_index in range(u_segments):
        next_u = (u_index + 1) % count
        for v_index in range(row - 1):
            faces.append((
                u_index * row + v_index,
                next_u * row + v_index,
                next_u * row + v_index + 1,
                u_index * row + v_index + 1,
            ))

    return verts, faces, uvs, row, count


def rebuild_curve_deformed_mesh_from_points(frill_obj, attach_points, tip_points, is_loop, settings):
    verts, faces, uvs, row, point_count = build_curve_deformed_mesh_data_from_points(attach_points, tip_points, is_loop, settings)

    mesh = frill_obj.data
    mesh.clear_geometry()
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])
    mesh.from_pydata([tuple(vertex) for vertex in verts], [], faces)
    mesh.update()
    smooth_shading = bool(settings.smooth_shading)
    for polygon in mesh.polygons:
        polygon.use_smooth = smooth_shading

    uv_layer = mesh.uv_layers.new(name="Frill_UV")
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            uv_layer.data[loop_index].uv = uvs[mesh.loops[loop_index].vertex_index]

    while frill_obj.vertex_groups:
        frill_obj.vertex_groups.remove(frill_obj.vertex_groups[0])

    all_group = frill_obj.vertex_groups.new(name=ALL_GROUP)
    pin_group = frill_obj.vertex_groups.new(name=PIN_GROUP)
    tip_group = frill_obj.vertex_groups.new(name=TIP_GROUP)
    all_indices = list(range(len(verts)))
    pin_indices = [u_index * row for u_index in range(point_count)]
    tip_indices = [u_index * row + row - 1 for u_index in range(point_count)]
    all_group.add(all_indices, 1.0, 'ADD')
    pin_group.add(pin_indices, 1.0, 'ADD')
    tip_group.add(tip_indices, 1.0, 'ADD')
    frill_obj["frill_curve_sample_count"] = point_count


def strip_mesh_settings_from_object(frill_obj):
    return SimpleNamespace(
        resolution_v=max(1, int(frill_obj.get("frill_resolution_v", 3))),
        width=float(frill_obj.get("frill_width", 0.2)),
        gather_smoothing_enabled=bool(frill_obj.get("frill_gather_smoothing_enabled", False)),
        gather_shape_offset=float(frill_obj.get("frill_gather_shape_offset", 0.0)),
        smooth_shading=bool(frill_obj.get("frill_smooth_shading", True)),
        tip_wave_count=int(frill_obj.get("frill_tip_wave_count", 1)),
        positive_mid_depth=float(frill_obj.get("frill_positive_mid_depth", 0.0)),
        positive_mid_profile_power=float(frill_obj.get("frill_positive_mid_profile_power", 0.88)),
        positive_mid_center=float(frill_obj.get("frill_positive_mid_center", 0.5)),
        negative_mid_depth=float(frill_obj.get("frill_negative_mid_depth", 0.0)),
        negative_mid_profile_power=float(frill_obj.get("frill_negative_mid_profile_power", 0.88)),
        negative_mid_center=float(frill_obj.get("frill_negative_mid_center", 0.5)),
    )


def gather_curve_name_from_frill(frill_obj):
    return frill_obj.get("frill_gather_curve", "")


def gather_curve_from_frill(frill_obj):
    return bpy.data.objects.get(gather_curve_name_from_frill(frill_obj))


def apply_strip_curve_mesh(frill_obj, gather_curve, tip_curve):
    use_edit_curve_points = (
        frill_obj.get("frill_type", "") == EDIT_MODE_IDENTIFIER
        and bool(frill_obj.get("frill_edit_linear_mesh_from_curve_points", False))
    )
    if use_edit_curve_points:
        attach_points = curve_control_points_from_object(gather_curve)
        tip_points = curve_control_points_from_object(tip_curve)
    else:
        sample_count = max(
            curve_sample_count_from_resolution(gather_curve),
            curve_sample_count_from_resolution(tip_curve),
        )
        attach_points = sample_bezier_curve_object(gather_curve, sample_count)
        tip_points = sample_bezier_curve_object(tip_curve, sample_count)
    settings = strip_mesh_settings_from_object(frill_obj)
    is_loop = bool(frill_obj.get("frill_is_loop", False))
    rebuild_curve_deformed_mesh_from_points(frill_obj, attach_points, tip_points, is_loop, settings)


def ensure_curve_modifier(frill_obj, source_curve, settings):
    modifier = frill_obj.modifiers.get(CURVE_MODIFIER_NAME)
    if modifier is None or modifier.type != 'CURVE':
        modifier = frill_obj.modifiers.new(CURVE_MODIFIER_NAME, 'CURVE')
    modifier.object = source_curve
    modifier.deform_axis = settings.curve_deform_axis
    modifier.show_viewport = settings.curve_modifier_show_viewport
    modifier.show_render = settings.curve_modifier_show_viewport
    return modifier


def create_curve_driven_frill_mesh(context, source_obj, points, columns, is_loop, settings, source_vertex_order, source_edges):
    source_points = source_curve_points_for_settings(points, settings)
    source_tilts = source_curve_tilt_values(source_points, settings, is_loop)
    strip_length = source_curve_length(points, settings.length_ratio)
    curve_resolution = curve_resolution_u(settings)
    handle_scale = curve_handle_scale(settings)
    attach_points, tip_points = strip_curve_points_from_columns(columns, strip_length, is_loop, settings)
    attach_handle_controls, tip_handle_controls = strip_curve_handle_controls_from_columns(columns, settings)
    parent_collection, generated_collection = create_generated_frill_collection(context, source_obj)
    source_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_SourceCurve", source_points, is_loop, generated_collection, curve_resolution, handle_scale, tilt_values=source_tilts, handle_type=settings.source_curve_handle_type)
    gather_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_GatherCurve", attach_points, is_loop, generated_collection, curve_resolution, handle_scale, handle_controls=attach_handle_controls)
    tip_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_TipCurve", tip_points, is_loop, generated_collection, curve_resolution, handle_scale, handle_controls=tip_handle_controls)

    mesh = bpy.data.meshes.new(f"{source_obj.name}_FrillMesh")
    frill_obj = bpy.data.objects.new(f"{source_obj.name}_Frill", mesh)
    generated_collection.objects.link(frill_obj)

    frill_obj["frill_curve_sample_count"] = max(2, len(columns) * curve_resolution)
    frill_obj["frill_source_curve"] = source_curve.name
    frill_obj["frill_gather_curve"] = gather_curve.name
    frill_obj["frill_tip_curve"] = tip_curve.name
    frill_obj["frill_parent_collection"] = parent_collection.name
    frill_obj["frill_generated_collection"] = generated_collection.name
    frill_obj["frill_curve_driven"] = True
    frill_obj["frill_uses_curve_modifier"] = True
    frill_obj["frill_gather_smoothing_enabled"] = settings.gather_smoothing_enabled
    frill_obj["frill_smooth_shading"] = settings.smooth_shading
    source_curve["frill_mesh_object"] = frill_obj.name
    gather_curve["frill_mesh_object"] = frill_obj.name
    tip_curve["frill_mesh_object"] = frill_obj.name
    source_curve.data["frill_mesh_object"] = frill_obj.name
    gather_curve.data["frill_mesh_object"] = frill_obj.name
    tip_curve.data["frill_mesh_object"] = frill_obj.name
    source_curve["frill_curve_role"] = "SOURCE"
    gather_curve["frill_curve_role"] = "GATHER"
    tip_curve["frill_curve_role"] = "TIP"
    source_curve.data["frill_curve_role"] = "SOURCE"
    gather_curve.data["frill_curve_role"] = "GATHER"
    tip_curve.data["frill_curve_role"] = "TIP"
    ensure_curve_modifier(frill_obj, source_curve, settings)
    apply_strip_curve_mesh(frill_obj, gather_curve, tip_curve)
    write_frill_properties(frill_obj, source_obj, columns, is_loop, settings, source_vertex_order, source_edges)

    context.view_layer.objects.active = frill_obj
    frill_obj.select_set(True)
    source_obj.select_set(False)
    source_curve.hide_select = False
    gather_curve.hide_select = False
    tip_curve.hide_select = False
    return frill_obj


def update_curve_driven_frill_from_params(context, frill_obj, settings):
    points, is_loop, source_obj, source_vertex_order, source_edges = source_points_from_frill(frill_obj)
    columns = build_curve_control_frill_shape(points, is_loop, settings)
    source_points = source_curve_points_for_settings(points, settings)
    source_tilts = source_curve_tilt_values(source_points, settings, is_loop)
    strip_length = source_curve_length(points, settings.length_ratio)
    attach_points, tip_points = strip_curve_points_from_columns(columns, strip_length, is_loop, settings)
    attach_handle_controls, tip_handle_controls = strip_curve_handle_controls_from_columns(columns, settings)

    source_curve = bpy.data.objects.get(frill_obj.get("frill_source_curve", ""))
    gather_curve = gather_curve_from_frill(frill_obj)
    tip_curve = bpy.data.objects.get(frill_obj.get("frill_tip_curve", ""))
    curve_resolution = curve_resolution_u(settings)
    handle_scale = curve_handle_scale(settings)
    if source_curve is None:
        target_collection = generated_frill_collection(frill_obj, context.collection)
        source_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_SourceCurve", source_points, is_loop, target_collection, curve_resolution, handle_scale, tilt_values=source_tilts, handle_type=settings.source_curve_handle_type)
        source_curve["frill_mesh_object"] = frill_obj.name
        source_curve.data["frill_mesh_object"] = frill_obj.name
        source_curve["frill_curve_role"] = "SOURCE"
        source_curve.data["frill_curve_role"] = "SOURCE"
        frill_obj["frill_source_curve"] = source_curve.name

    if gather_curve is None:
        target_collection = generated_frill_collection(frill_obj, context.collection)
        gather_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_GatherCurve", attach_points, is_loop, target_collection, curve_resolution, handle_scale, handle_controls=attach_handle_controls)
        gather_curve["frill_mesh_object"] = frill_obj.name
        gather_curve.data["frill_mesh_object"] = frill_obj.name
        gather_curve["frill_curve_role"] = "GATHER"
        gather_curve.data["frill_curve_role"] = "GATHER"
        frill_obj["frill_gather_curve"] = gather_curve.name
    else:
        gather_curve["frill_curve_role"] = "GATHER"
        gather_curve.data["frill_curve_role"] = "GATHER"
        frill_obj["frill_gather_curve"] = gather_curve.name

    if tip_curve is None:
        target_collection = generated_frill_collection(frill_obj, context.collection)
        tip_curve = create_bezier_curve_object(f"{source_obj.name}_Frill_TipCurve", tip_points, is_loop, target_collection, curve_resolution, handle_scale, handle_controls=tip_handle_controls)
        tip_curve["frill_mesh_object"] = frill_obj.name
        tip_curve.data["frill_mesh_object"] = frill_obj.name
        tip_curve["frill_curve_role"] = "TIP"
        tip_curve.data["frill_curve_role"] = "TIP"
        frill_obj["frill_tip_curve"] = tip_curve.name

    set_bezier_curve_points(source_curve, source_points, is_loop, curve_resolution, handle_scale, tilt_values=source_tilts, handle_type=settings.source_curve_handle_type)
    set_bezier_curve_points(gather_curve, attach_points, is_loop, curve_resolution, handle_scale, handle_controls=attach_handle_controls)
    set_bezier_curve_points(tip_curve, tip_points, is_loop, curve_resolution, handle_scale, handle_controls=tip_handle_controls)
    ensure_curve_modifier(frill_obj, source_curve, settings)
    apply_strip_curve_mesh(frill_obj, gather_curve, tip_curve)
    frill_obj["frill_gather_smoothing_enabled"] = settings.gather_smoothing_enabled
    frill_obj["frill_smooth_shading"] = settings.smooth_shading
    frill_obj["frill_curve_sample_count"] = max(2, len(columns) * curve_resolution)
    write_frill_properties(frill_obj, source_obj, columns, is_loop, settings, source_vertex_order, source_edges)


def write_frill_properties(frill_obj, source_obj, columns, is_loop, settings, source_vertex_order, source_edges):
    frill_obj["frill_source_object"] = source_obj.name
    frill_obj["frill_type"] = settings.frill_type
    preset_clean = current_settings_match_selected_preset(settings)
    frill_obj["frill_preset_clean"] = preset_clean
    frill_obj["frill_preset_identifier"] = settings.frill_type if preset_clean else ""
    frill_obj["frill_width"] = settings.width
    frill_obj["frill_height"] = settings.width
    frill_obj["frill_resolution_u"] = curve_resolution_u(settings)
    frill_obj["frill_resolution_v"] = settings.resolution_v
    frill_obj["frill_seed"] = settings.seed
    frill_obj["frill_randomness"] = settings.randomness
    frill_obj["frill_is_loop"] = is_loop
    frill_obj["frill_length_ratio"] = settings.length_ratio
    frill_obj["frill_direction_angle"] = settings.direction_angle
    frill_obj["frill_angle_direct_control"] = settings.angle_direct_control
    angle_point_count = len(source_vertex_order) if settings.angle_direct_control else settings.angle_point_count
    angle_point_count = max(1, min(ANGLE_POINT_MAX, int(angle_point_count)))
    frill_obj["frill_angle_point_count"] = angle_point_count
    for key in list(frill_obj.keys()):
        if is_numbered_frill_angle_point_property(key):
            del frill_obj[key]
    for index in range(2, angle_point_count + 1):
        frill_obj[f"frill_angle_point_{index:04d}"] = angle_point_value(settings, index - 1)
    frill_obj["frill_edit_curve_point_count"] = edit_curve_point_count(settings, len(columns), is_loop) if is_edit_mode(settings) else settings.edit_curve_point_count
    frill_obj["frill_edit_linear_mesh_from_curve_points"] = settings.edit_linear_mesh_from_curve_points
    frill_obj["frill_curve_modifier_show_viewport"] = settings.curve_modifier_show_viewport
    frill_obj["frill_curve_deform_axis"] = settings.curve_deform_axis
    frill_obj["frill_reverse_source_curve"] = settings.reverse_source_curve
    frill_obj["frill_source_curve_handle_type"] = settings.source_curve_handle_type
    frill_obj["frill_turn_mesh_x"] = settings.turn_mesh_x
    frill_obj["frill_direction_vector"] = tuple(columns[0]["outward"]) if columns else (0.0, 0.0, 0.0)
    frill_obj["frill_gather_smoothing_enabled"] = settings.gather_smoothing_enabled
    frill_obj["frill_gather_shape_offset"] = settings.gather_shape_offset
    frill_obj["frill_smooth_shading"] = settings.smooth_shading
    frill_obj["frill_tip_wave_count"] = settings.tip_wave_count
    frill_obj["frill_positive_mid_center"] = settings.positive_mid_center
    frill_obj["frill_positive_mid_depth"] = settings.positive_mid_depth
    frill_obj["frill_positive_mid_profile_power"] = settings.positive_mid_profile_power
    frill_obj["frill_negative_mid_center"] = settings.negative_mid_center
    frill_obj["frill_negative_mid_depth"] = settings.negative_mid_depth
    frill_obj["frill_negative_mid_profile_power"] = settings.negative_mid_profile_power
    frill_obj["frill_cycle_pair_negative_in_out"] = settings.cycle_pair_negative_in_out
    frill_obj["frill_cycle_pair_positive_in_out"] = settings.cycle_pair_positive_in_out
    frill_obj["frill_cycle_random_enabled"] = settings.cycle_random_enabled
    frill_obj["frill_cycle_random_seed"] = settings.cycle_random_seed
    frill_obj["frill_cycle_random_move_offset"] = settings.cycle_random_move_offset
    frill_obj["frill_cycle_random_handle_offset"] = settings.cycle_random_handle_offset
    frill_obj["frill_cycle_random_rotation_offset"] = settings.cycle_random_rotation_offset
    frill_obj["frill_cycle_all_depth_offset"] = settings.cycle_all_depth_offset
    frill_obj["frill_gather_pair_negative_in_out"] = settings.gather_pair_negative_in_out
    frill_obj["frill_gather_pair_positive_in_out"] = settings.gather_pair_positive_in_out
    frill_obj["frill_gather_random_enabled"] = settings.gather_random_enabled
    frill_obj["frill_gather_random_seed"] = settings.gather_random_seed
    frill_obj["frill_gather_random_move_offset"] = settings.gather_random_move_offset
    frill_obj["frill_gather_random_handle_offset"] = settings.gather_random_handle_offset
    frill_obj["frill_gather_random_rotation_offset"] = settings.gather_random_rotation_offset
    frill_obj["frill_gather_all_depth_offset"] = settings.gather_all_depth_offset
    for point_key, _label in LINE_CONTROL_POINT_DEFINITIONS:
        frill_obj[f"frill_gather_{point_key}_move_x"] = getattr(settings, f"gather_{point_key}_move_x")
        frill_obj[f"frill_gather_{point_key}_move_y"] = getattr(settings, f"gather_{point_key}_move_y")
        frill_obj[f"frill_gather_{point_key}_move_z"] = getattr(settings, f"gather_{point_key}_move_z")
        frill_obj[f"frill_gather_{point_key}_handle_length"] = getattr(settings, f"gather_{point_key}_handle_length")
        frill_obj[f"frill_gather_{point_key}_handle_rotation"] = getattr(settings, f"gather_{point_key}_handle_rotation")
        frill_obj[f"frill_{point_key}_move_x"] = getattr(settings, f"{point_key}_move_x")
        frill_obj[f"frill_{point_key}_move_y"] = getattr(settings, f"{point_key}_move_y")
        frill_obj[f"frill_{point_key}_move_z"] = getattr(settings, f"{point_key}_move_z")
        frill_obj[f"frill_{point_key}_handle_length"] = getattr(settings, f"{point_key}_handle_length")
        frill_obj[f"frill_{point_key}_handle_rotation"] = getattr(settings, f"{point_key}_handle_rotation")
    frill_obj["frill_tip_lateral_amplitude"] = settings.tip_lateral_amplitude
    frill_obj["frill_tip_lateral_frequency"] = settings.tip_lateral_frequency
    frill_obj["frill_tip_lateral_phase"] = settings.tip_lateral_phase
    frill_obj["frill_tip_lateral_randomness"] = settings.tip_lateral_randomness
    frill_obj["frill_auto_update"] = settings.auto_update
    frill_obj["frill_source_vertex_order"] = source_vertex_order
    frill_obj["frill_source_edges"] = source_edges


_is_auto_updating = False
_is_curve_mesh_updating = False
_pending_frill_rebuilds = {}
_last_frill_rebuild_request_time = 0.0
_last_selected_frill_name = None
DEFAULT_SETTINGS = {
    "width": 0.2,
    "resolution_u": 32,
    "resolution_v": 3,
    "frill_type": "Straight",
    "length_ratio": 1.0,
    "direction_angle": 0.0,
    "angle_direct_control": False,
    "angle_point_count": 1,
    "edit_curve_point_count": 4,
    "edit_linear_mesh_from_curve_points": False,
    "curve_modifier_show_viewport": True,
    "curve_deform_axis": 'POS_X',
    "reverse_source_curve": False,
    "source_curve_handle_type": 'VECTOR',
    "turn_mesh_x": False,
    "gather_smoothing_enabled": False,
    "gather_shape_offset": 0.0,
    "tip_wave_count": 6,
    "positive_mid_center": 0.5,
    "positive_mid_depth": 0.0,
    "positive_mid_profile_power": 0.88,
    "negative_mid_center": 0.5,
    "negative_mid_depth": 0.0,
    "negative_mid_profile_power": 0.88,
    "cycle_pair_positive_in_out": False,
    "cycle_pair_negative_in_out": False,
    "cycle_random_enabled": False,
    "cycle_random_seed": 1,
    "cycle_random_move_offset": 0.0,
    "cycle_random_handle_offset": 0.0,
    "cycle_random_rotation_offset": 0.0,
    "cycle_all_depth_offset": 0.0,
    "gather_pair_positive_in_out": False,
    "gather_pair_negative_in_out": False,
    "gather_random_enabled": False,
    "gather_random_seed": 1,
    "gather_random_move_offset": 0.0,
    "gather_random_handle_offset": 0.0,
    "gather_random_rotation_offset": 0.0,
    "gather_all_depth_offset": 0.0,
    "gather_negative_in_move_x": 0.0,
    "gather_negative_in_move_y": 0.0,
    "gather_negative_in_move_z": 0.0,
    "gather_negative_in_handle_length": 1.0,
    "gather_negative_in_handle_rotation": 0.0,
    "gather_negative_trough_move_x": 0.0,
    "gather_negative_trough_move_y": 0.0,
    "gather_negative_trough_move_z": 0.0,
    "gather_negative_trough_handle_length": 1.0,
    "gather_negative_trough_handle_rotation": 0.0,
    "gather_negative_out_move_x": 0.0,
    "gather_negative_out_move_y": 0.0,
    "gather_negative_out_move_z": 0.0,
    "gather_negative_out_handle_length": 1.0,
    "gather_negative_out_handle_rotation": 0.0,
    "gather_positive_in_move_x": 0.0,
    "gather_positive_in_move_y": 0.0,
    "gather_positive_in_move_z": 0.0,
    "gather_positive_in_handle_length": 1.0,
    "gather_positive_in_handle_rotation": 0.0,
    "gather_positive_peak_move_x": 0.0,
    "gather_positive_peak_move_y": 0.0,
    "gather_positive_peak_move_z": 0.0,
    "gather_positive_peak_handle_length": 1.0,
    "gather_positive_peak_handle_rotation": 0.0,
    "gather_positive_out_move_x": 0.0,
    "gather_positive_out_move_y": 0.0,
    "gather_positive_out_move_z": 0.0,
    "gather_positive_out_handle_length": 1.0,
    "gather_positive_out_handle_rotation": 0.0,
    "negative_in_move_x": 0.0,
    "negative_in_move_y": 0.0,
    "negative_in_move_z": 0.0,
    "negative_in_handle_length": 1.0,
    "negative_in_handle_rotation": 0.0,
    "negative_trough_move_x": 0.0,
    "negative_trough_move_y": 0.0,
    "negative_trough_move_z": 0.0,
    "negative_trough_handle_length": 1.0,
    "negative_trough_handle_rotation": 0.0,
    "negative_out_move_x": 0.0,
    "negative_out_move_y": 0.0,
    "negative_out_move_z": 0.0,
    "negative_out_handle_length": 1.0,
    "negative_out_handle_rotation": 0.0,
    "positive_in_move_x": 0.0,
    "positive_in_move_y": 0.0,
    "positive_in_move_z": 0.0,
    "positive_in_handle_length": 1.0,
    "positive_in_handle_rotation": 0.0,
    "positive_peak_move_x": 0.0,
    "positive_peak_move_y": 0.0,
    "positive_peak_move_z": 0.0,
    "positive_peak_handle_length": 1.0,
    "positive_peak_handle_rotation": 0.0,
    "positive_out_move_x": 0.0,
    "positive_out_move_y": 0.0,
    "positive_out_move_z": 0.0,
    "positive_out_handle_length": 1.0,
    "positive_out_handle_rotation": 0.0,
    "tip_lateral_amplitude": 0.0,
    "tip_lateral_frequency": 1.0,
    "tip_lateral_phase": 0.0,
    "tip_lateral_randomness": 0.0,
    "seed": 1,
    "randomness": 0.0,
    "smooth_shading": True,
    "auto_update": True,
    "update_sec": 0.0,
}
PRESET_DIRECTORY_NAME = "Preset"
PRESET_FILE_SUFFIX = ".json"
DEFAULT_PRESET_NAMES = ("Straight", "Gather")
NO_PRESET_IDENTIFIER = "__NO_PRESETS__"
PRESET_BASE_SETTING_KEYS = (
    "width",
    "length_ratio",
    "resolution_v",
    "resolution_u",
    "tip_wave_count",
    "source_curve_handle_type",
    "angle_direct_control",
    "angle_point_count",
    "direction_angle",
    "turn_mesh_x",
    "curve_deform_axis",
    "reverse_source_curve",
    "gather_smoothing_enabled",
    "gather_shape_offset",
    "tip_lateral_amplitude",
    "tip_lateral_frequency",
    "tip_lateral_phase",
    "tip_lateral_randomness",
    "randomness",
    "seed",
    "positive_mid_center",
    "positive_mid_depth",
    "positive_mid_profile_power",
    "negative_mid_center",
    "negative_mid_depth",
    "negative_mid_profile_power",
    "cycle_pair_positive_in_out",
    "cycle_pair_negative_in_out",
    "cycle_random_enabled",
    "cycle_random_seed",
    "cycle_random_move_offset",
    "cycle_random_handle_offset",
    "cycle_random_rotation_offset",
    "cycle_all_depth_offset",
    *(f"{point_key}_{suffix}" for point_key, _label in LINE_CONTROL_UI_ORDER for suffix in ("move_x", "move_y", "move_z", "handle_length", "handle_rotation")),
    "gather_pair_positive_in_out",
    "gather_pair_negative_in_out",
    "gather_random_enabled",
    "gather_random_seed",
    "gather_random_move_offset",
    "gather_random_handle_offset",
    "gather_random_rotation_offset",
    "gather_all_depth_offset",
    *(f"gather_{point_key}_{suffix}" for point_key, _label in LINE_CONTROL_UI_ORDER for suffix in ("move_x", "move_y", "move_z", "handle_length", "handle_rotation")),
    "edit_curve_point_count",
    "edit_linear_mesh_from_curve_points",
)
PRESET_COMPAT_DEFAULT_KEYS = {
    "angle_point_count",
    "angle_direct_control",
    "edit_curve_point_count",
    "edit_linear_mesh_from_curve_points",
    "curve_deform_axis",
    "reverse_source_curve",
    "source_curve_handle_type",
    "turn_mesh_x",
}
PRESET_IGNORED_SETTING_KEYS = {
    "frill_type",
    "smooth_shading",
    "auto_update",
    "update_sec",
    "curve_modifier_show_viewport",
}
_PRESET_CACHE = {}
_PRESET_ENUM_ITEMS = []


SETTING_OBJECT_PROPERTY_NAMES = {
    "width": "frill_width",
    "resolution_u": "frill_resolution_u",
    "resolution_v": "frill_resolution_v",
    "frill_type": "frill_type",
    "length_ratio": "frill_length_ratio",
    "direction_angle": "frill_direction_angle",
    "angle_direct_control": "frill_angle_direct_control",
    "angle_point_count": "frill_angle_point_count",
    "edit_curve_point_count": "frill_edit_curve_point_count",
    "edit_linear_mesh_from_curve_points": "frill_edit_linear_mesh_from_curve_points",
    "curve_modifier_show_viewport": "frill_curve_modifier_show_viewport",
    "curve_deform_axis": "frill_curve_deform_axis",
    "reverse_source_curve": "frill_reverse_source_curve",
    "source_curve_handle_type": "frill_source_curve_handle_type",
    "turn_mesh_x": "frill_turn_mesh_x",
    "gather_smoothing_enabled": "frill_gather_smoothing_enabled",
    "gather_shape_offset": "frill_gather_shape_offset",
    "smooth_shading": "frill_smooth_shading",
    "auto_update": "frill_auto_update",
    "tip_wave_count": "frill_tip_wave_count",
    "positive_mid_center": "frill_positive_mid_center",
    "positive_mid_depth": "frill_positive_mid_depth",
    "positive_mid_profile_power": "frill_positive_mid_profile_power",
    "negative_mid_center": "frill_negative_mid_center",
    "negative_mid_depth": "frill_negative_mid_depth",
    "negative_mid_profile_power": "frill_negative_mid_profile_power",
    "tip_lateral_amplitude": "frill_tip_lateral_amplitude",
    "tip_lateral_frequency": "frill_tip_lateral_frequency",
    "tip_lateral_phase": "frill_tip_lateral_phase",
    "tip_lateral_randomness": "frill_tip_lateral_randomness",
    "randomness": "frill_randomness",
    "seed": "frill_seed",
    "cycle_pair_positive_in_out": "frill_cycle_pair_positive_in_out",
    "cycle_pair_negative_in_out": "frill_cycle_pair_negative_in_out",
    "cycle_random_enabled": "frill_cycle_random_enabled",
    "cycle_random_seed": "frill_cycle_random_seed",
    "cycle_random_move_offset": "frill_cycle_random_move_offset",
    "cycle_random_handle_offset": "frill_cycle_random_handle_offset",
    "cycle_random_rotation_offset": "frill_cycle_random_rotation_offset",
    "cycle_all_depth_offset": "frill_cycle_all_depth_offset",
    "gather_pair_positive_in_out": "frill_gather_pair_positive_in_out",
    "gather_pair_negative_in_out": "frill_gather_pair_negative_in_out",
    "gather_random_enabled": "frill_gather_random_enabled",
    "gather_random_seed": "frill_gather_random_seed",
    "gather_random_move_offset": "frill_gather_random_move_offset",
    "gather_random_handle_offset": "frill_gather_random_handle_offset",
    "gather_random_rotation_offset": "frill_gather_random_rotation_offset",
    "gather_all_depth_offset": "frill_gather_all_depth_offset",
}

for _point_key, _label in LINE_CONTROL_POINT_DEFINITIONS:
    for _suffix in ("move_x", "move_y", "move_z", "handle_length", "handle_rotation"):
        SETTING_OBJECT_PROPERTY_NAMES[f"{_point_key}_{_suffix}"] = f"frill_{_point_key}_{_suffix}"
        SETTING_OBJECT_PROPERTY_NAMES[f"gather_{_point_key}_{_suffix}"] = f"frill_gather_{_point_key}_{_suffix}"


def addon_preferences():
    preferences = getattr(bpy.context, "preferences", None)
    if preferences is None:
        return None

    addon_keys = [key for key in {__package__, __name__} if key]
    for addon_key in addon_keys:
        addon = preferences.addons.get(addon_key)
        if addon is not None:
            return addon.preferences
    return None


def bundled_preset_directory():
    return Path(__file__).resolve().parent / PRESET_DIRECTORY_NAME


def preset_directory():
    preferences = addon_preferences()
    custom_directory = getattr(preferences, "preset_directory", "") if preferences is not None else ""
    if custom_directory:
        return Path(bpy.path.abspath(custom_directory)).expanduser()
    return bundled_preset_directory()


def ensure_preset_directory():
    folder = preset_directory()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_default_presets_in_folder(folder):
    source_folder = bundled_preset_directory()
    created = []
    for preset_name in DEFAULT_PRESET_NAMES:
        source_path = source_folder / f"{preset_name}{PRESET_FILE_SUFFIX}"
        target_path = folder / f"{preset_name}{PRESET_FILE_SUFFIX}"
        if target_path.exists():
            continue
        if not source_path.exists():
            continue
        target_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(target_path.name)
    return created


def preset_enum_items(_self, _context):
    if _PRESET_ENUM_ITEMS:
        return _PRESET_ENUM_ITEMS
    return [(NO_PRESET_IDENTIFIER, "No Presets", "No preset files were found."), EDIT_MODE_ITEM]


def reload_preset_cache():
    folder = ensure_preset_directory()
    loaded = {}
    items = []
    def preset_sort_key(path):
        priority = 0 if path.stem == "Straight" else 1
        return priority, path.stem.casefold()

    for path in sorted(folder.glob(f"*{PRESET_FILE_SUFFIX}"), key=preset_sort_key):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
            continue
        identifier = path.stem
        loaded[identifier] = {
            "path": path,
            "name": path.stem,
            "is_builtin": bool(payload.get("is_builtin", False)),
            "settings": payload["settings"],
        }
        items.append((identifier, path.stem, f"Load preset from {path.name}"))

    _PRESET_CACHE.clear()
    _PRESET_CACHE.update(loaded)
    _PRESET_ENUM_ITEMS[:] = items + [EDIT_MODE_ITEM]
    return len(items)


def show_preset_error(context, message):
    if context is None or context.window_manager is None:
        return

    def draw(self, _context):
        self.layout.label(text=message, translate=False)

    context.window_manager.popup_menu(draw, title="Preset Error", icon='ERROR')


def first_preset_identifier():
    for identifier, _name, _description in _PRESET_ENUM_ITEMS:
        if identifier != EDIT_MODE_IDENTIFIER:
            return identifier
    return NO_PRESET_IDENTIFIER


def ensure_valid_preset_selection(settings):
    if settings.frill_type == EDIT_MODE_IDENTIFIER:
        return
    if settings.frill_type in _PRESET_CACHE:
        return
    fallback = first_preset_identifier()
    if fallback != NO_PRESET_IDENTIFIER:
        settings.frill_type = fallback


def read_selected_preset(identifier):
    if identifier == EDIT_MODE_IDENTIFIER:
        return None
    preset = _PRESET_CACHE.get(identifier)
    if preset is None:
        return None
    path = preset["path"]
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        return None
    preset["is_builtin"] = bool(payload.get("is_builtin", False))
    preset["settings"] = payload["settings"]
    return preset


def preset_setting_names(settings, context=None):
    names = [name for name in PRESET_BASE_SETTING_KEYS if hasattr(settings, name)]
    if settings.angle_direct_control:
        if "angle_point_count" in names:
            names.remove("angle_point_count")
        angle_count = angle_point_count_for_ui(context, settings)
    else:
        angle_count = max(1, min(ANGLE_POINT_MAX, settings.angle_point_count))

    angle_insert_index = names.index("direction_angle") + 1 if "direction_angle" in names else len(names)
    angle_names = [
        f"angle_point_{index:04d}"
        for index in range(2, angle_count + 1)
        if hasattr(settings, f"angle_point_{index:04d}")
    ]
    names[angle_insert_index:angle_insert_index] = angle_names
    return names


def setting_values_match(actual, expected):
    if isinstance(actual, float) or isinstance(expected, float):
        try:
            return abs(float(actual) - float(expected)) <= 0.000001
        except (TypeError, ValueError):
            return False
    return actual == expected


def current_settings_match_preset(settings, preset_settings, context=None):
    for name in preset_setting_names(settings, context):
        if name in PRESET_IGNORED_SETTING_KEYS or not hasattr(settings, name):
            continue
        if name in preset_settings:
            expected = preset_settings[name]
        elif name in DEFAULT_SETTINGS:
            expected = DEFAULT_SETTINGS[name]
        else:
            continue
        if not setting_values_match(getattr(settings, name), expected):
            return False
    return True


def current_settings_match_selected_preset(settings, context=None):
    if settings.frill_type == EDIT_MODE_IDENTIFIER:
        return False

    preset = read_selected_preset(settings.frill_type)
    if preset is None:
        return False
    return current_settings_match_preset(settings, preset["settings"], context)


def apply_preset_settings(settings, preset_settings):
    for index in range(2, ANGLE_POINT_MAX + 1):
        name = f"angle_point_{index:04d}"
        if hasattr(settings, name):
            setattr(settings, name, 0.0)

    for name in PRESET_BASE_SETTING_KEYS:
        if name in PRESET_IGNORED_SETTING_KEYS or not hasattr(settings, name):
            continue
        if name in preset_settings:
            setattr(settings, name, preset_settings[name])
        elif name in PRESET_COMPAT_DEFAULT_KEYS:
            setattr(settings, name, DEFAULT_SETTINGS[name])

    preset_angle_count = preset_settings.get("angle_point_count")
    if preset_angle_count is None:
        preset_angle_count = 1
        for name in preset_settings:
            if name.startswith("angle_point_"):
                try:
                    preset_angle_count = max(preset_angle_count, int(name.rsplit("_", 1)[1]))
                except (TypeError, ValueError):
                    pass
    angle_count = max(1, min(ANGLE_POINT_MAX, int(preset_angle_count)))
    for index in range(2, angle_count + 1):
        name = f"angle_point_{index:04d}"
        if name in preset_settings and hasattr(settings, name):
            setattr(settings, name, preset_settings[name])


def apply_straight_preset_settings(settings):
    preset = read_selected_preset("Straight")
    if preset is not None:
        settings.frill_type = "Straight"
        apply_preset_settings(settings, preset["settings"])
        settings.frill_preset_unsaved = False
        synchronize_mirrored_point_settings(settings)
        return True

    for name, value in DEFAULT_SETTINGS.items():
        if hasattr(settings, name):
            setattr(settings, name, value)
    if "Straight" in _PRESET_CACHE:
        settings.frill_type = "Straight"
    settings.frill_preset_unsaved = False
    synchronize_mirrored_point_settings(settings)
    return False


def safe_preset_stem(raw_name):
    invalid_chars = '<>:"/\\|?*'
    cleaned = ''.join("_" if char in invalid_chars else char for char in raw_name).strip().rstrip(".")
    return cleaned or "Preset"


def preset_path_for_name(raw_name):
    return ensure_preset_directory() / f"{safe_preset_stem(raw_name)}{PRESET_FILE_SUFFIX}"


def scene_has_curve_driven_frills(scene):
    return any(
        obj.type == 'MESH' and obj.get("frill_curve_driven", False)
        for obj in scene.objects
    )


def initialize_scene_preset_defaults():
    global _is_auto_updating
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return 0.1

    preferred_identifier = "Straight" if "Straight" in _PRESET_CACHE else first_preset_identifier()
    if preferred_identifier == NO_PRESET_IDENTIFIER:
        return None

    preset = read_selected_preset(preferred_identifier)
    if preset is None:
        return None

    previous = _is_auto_updating
    _is_auto_updating = True
    try:
        for scene in scenes:
            if scene_has_curve_driven_frills(scene):
                continue
            settings = scene.frillcreate_settings
            settings.frill_type = preferred_identifier
            apply_preset_settings(settings, preset["settings"])
            settings.frill_preset_unsaved = False
            synchronize_mirrored_point_settings(settings)
    finally:
        _is_auto_updating = previous
    return None


def schedule_scene_preset_defaults():
    if not bpy.app.timers.is_registered(initialize_scene_preset_defaults):
        bpy.app.timers.register(initialize_scene_preset_defaults, first_interval=0.0)


def frill_object_from_context(context):
    obj = context.object
    if obj and obj.type == 'MESH' and "frill_source_object" in obj:
        return obj
    if obj and obj.type == 'CURVE' and "frill_mesh_object" in obj:
        return bpy.data.objects.get(obj["frill_mesh_object"])

    for selected in context.selected_objects:
        if selected.type == 'MESH' and "frill_source_object" in selected:
            return selected
        if selected.type == 'CURVE' and "frill_mesh_object" in selected:
            frill_obj = bpy.data.objects.get(selected["frill_mesh_object"])
            if frill_obj:
                return frill_obj

    return None


def active_frill_mesh_from_context(context):
    obj = context.object
    if obj and obj.type == 'MESH' and obj.get("frill_curve_driven", False):
        return obj
    return None


def source_points_from_frill(frill_obj):
    source_obj = bpy.data.objects.get(frill_obj.get("frill_source_object", ""))
    if source_obj is None or source_obj.type != 'MESH':
        raise ValueError("The original frill source mesh was not found.")

    source_vertex_order = [int(index) for index in frill_obj.get("frill_source_vertex_order", [])]
    if len(source_vertex_order) < 2:
        raise ValueError("The frill source vertex order is missing.")

    mesh = source_obj.data
    if any(index < 0 or index >= len(mesh.vertices) for index in source_vertex_order):
        raise ValueError("The frill source vertex order is no longer valid.")

    matrix = source_obj.matrix_world
    points = [matrix @ mesh.vertices[index].co for index in source_vertex_order]
    is_loop = bool(frill_obj.get("frill_is_loop", False))
    source_edges = list(frill_obj.get("frill_source_edges", []))
    return points, is_loop, source_obj, source_vertex_order, source_edges


def rebuild_frill_object(context, frill_obj, settings):
    if frill_obj.get("frill_keep_curve_applied", False):
        raise ValueError("This frill has already been applied with Keep Curve.")
    if not frill_obj.get("frill_curve_driven", False):
        raise ValueError("The active frill is already applied or is not curve-driven.")
    update_curve_driven_frill_from_params(context, frill_obj, settings)


def is_numbered_frill_angle_point_property(key):
    if not key.startswith("frill_angle_point_"):
        return False
    return key.rsplit("_", 1)[-1].isdigit()


def frill_angle_point_count_from_properties(frill_obj):
    if "frill_angle_point_count" in frill_obj:
        try:
            return max(1, min(ANGLE_POINT_MAX, int(frill_obj["frill_angle_point_count"])))
        except (TypeError, ValueError):
            pass

    angle_point_count = 1
    for key in frill_obj.keys():
        if not is_numbered_frill_angle_point_property(key):
            continue
        try:
            angle_point_count = max(angle_point_count, int(key.rsplit("_", 1)[1]))
        except (TypeError, ValueError):
            pass
    return max(1, min(ANGLE_POINT_MAX, angle_point_count))


def set_frill_curve_modifier_visibility(frill_obj, show_viewport):
    if frill_obj is None or frill_obj.type != 'MESH':
        return

    modifier = frill_obj.modifiers.get(CURVE_MODIFIER_NAME)
    if modifier is None or modifier.type != 'CURVE':
        return

    modifier.show_viewport = show_viewport
    modifier.show_render = show_viewport


def restore_angle_points_from_frill(settings, frill_obj):
    angle_point_count = frill_angle_point_count_from_properties(frill_obj)
    if hasattr(settings, "angle_point_count"):
        settings.angle_point_count = angle_point_count
    for index in range(2, angle_point_count + 1):
        property_name = f"frill_angle_point_{index:04d}"
        setting_name = f"angle_point_{index:04d}"
        if hasattr(settings, setting_name):
            setattr(settings, setting_name, float(frill_obj.get(property_name, 0.0)))
    for index in range(angle_point_count + 1, ANGLE_POINT_MAX + 1):
        setting_name = f"angle_point_{index:04d}"
        if hasattr(settings, setting_name):
            setattr(settings, setting_name, 0.0)


def restore_settings_from_frill_custom_properties(settings, frill_obj):
    for setting_name, property_name in SETTING_OBJECT_PROPERTY_NAMES.items():
        if not hasattr(settings, setting_name) or property_name not in frill_obj:
            continue
        if setting_name == "frill_type":
            frill_type = frill_obj[property_name]
            if frill_type != EDIT_MODE_IDENTIFIER and frill_type not in _PRESET_CACHE:
                continue
        if setting_name == "source_curve_handle_type" and frill_obj[property_name] not in {'ALIGNED', 'VECTOR'}:
            continue
        setattr(settings, setting_name, frill_obj[property_name])
    if "frill_angle_direct_control" not in frill_obj and hasattr(settings, "angle_direct_control"):
        settings.angle_direct_control = False
    if "frill_edit_linear_mesh_from_curve_points" not in frill_obj and hasattr(settings, "edit_linear_mesh_from_curve_points"):
        settings.edit_linear_mesh_from_curve_points = DEFAULT_SETTINGS["edit_linear_mesh_from_curve_points"]
    if "frill_source_curve_handle_type" not in frill_obj and hasattr(settings, "source_curve_handle_type"):
        settings.source_curve_handle_type = DEFAULT_SETTINGS["source_curve_handle_type"]
    if "frill_turn_mesh_x" not in frill_obj and "frill_flip_x_axis" in frill_obj and hasattr(settings, "turn_mesh_x"):
        settings.turn_mesh_x = bool(frill_obj["frill_flip_x_axis"])
    elif "frill_turn_mesh_x" not in frill_obj and "frill_flip_y_axis" in frill_obj and hasattr(settings, "turn_mesh_x"):
        settings.turn_mesh_x = bool(frill_obj["frill_flip_y_axis"])
    restore_angle_points_from_frill(settings, frill_obj)
    synchronize_mirrored_point_settings(settings)


def restore_scene_settings_from_frill_object(scene, frill_obj):
    if scene is None or frill_obj is None:
        return
    if not frill_obj.get("frill_curve_driven", False) or frill_obj.get("frill_keep_curve_applied", False):
        return

    settings = scene.frillcreate_settings
    preset_identifier = frill_obj.get("frill_preset_identifier", "")
    preset = read_selected_preset(preset_identifier) if frill_obj.get("frill_preset_clean", False) else None

    global _is_auto_updating
    previous = _is_auto_updating
    _is_auto_updating = True
    try:
        if preset is not None:
            settings.frill_type = preset_identifier
            apply_preset_settings(settings, preset["settings"])
            settings.frill_preset_unsaved = False
        else:
            restore_settings_from_frill_custom_properties(settings, frill_obj)
            settings.frill_preset_unsaved = True
    finally:
        _is_auto_updating = previous


def restore_settings_for_selected_frill(scene):
    global _last_selected_frill_name
    context = bpy.context
    frill_obj = active_frill_mesh_from_context(context)
    if frill_obj is None:
        _last_selected_frill_name = None
        return
    if frill_obj.name == _last_selected_frill_name:
        return

    _last_selected_frill_name = frill_obj.name
    restore_scene_settings_from_frill_object(scene, frill_obj)


def process_pending_frill_rebuilds():
    global _is_auto_updating, _last_frill_rebuild_request_time
    delay = max(
        0.0,
        min(
            MAX_UPDATE_DELAY_SECONDS,
            max((item["delay"] for item in _pending_frill_rebuilds.values()), default=0.0),
        ),
    )
    elapsed = time.monotonic() - _last_frill_rebuild_request_time
    if elapsed < delay:
        return max(0.01, delay - elapsed)

    pending = dict(_pending_frill_rebuilds)
    _pending_frill_rebuilds.clear()
    if not pending:
        return None

    previous = _is_auto_updating
    _is_auto_updating = True
    try:
        context = bpy.context
        for frill_name, item in pending.items():
            frill_obj = bpy.data.objects.get(frill_name)
            scene_name = item["scene"]
            scene = bpy.data.scenes.get(scene_name)
            if frill_obj is None or frill_obj.type != 'MESH' or scene is None:
                continue
            settings = scene.frillcreate_settings
            if not settings.auto_update:
                continue
            try:
                rebuild_frill_object(context, frill_obj, settings)
            except ValueError:
                continue
    finally:
        _is_auto_updating = previous

    if _pending_frill_rebuilds:
        _last_frill_rebuild_request_time = time.monotonic()
        return max(
            0.01,
            min(
                MAX_UPDATE_DELAY_SECONDS,
                max((item["delay"] for item in _pending_frill_rebuilds.values()), default=0.0),
            ),
        )
    return None


def schedule_frill_rebuild(context, frill_obj, delay):
    global _last_frill_rebuild_request_time
    scene = context.scene if context is not None else None
    if scene is None or frill_obj is None:
        return

    delay = max(0.0, min(MAX_UPDATE_DELAY_SECONDS, delay))
    _pending_frill_rebuilds[frill_obj.name] = {"scene": scene.name, "delay": delay}
    _last_frill_rebuild_request_time = time.monotonic()
    if not bpy.app.timers.is_registered(process_pending_frill_rebuilds):
        bpy.app.timers.register(process_pending_frill_rebuilds, first_interval=max(0.01, delay))


def synchronize_mirrored_point_pair(settings, prefix, source_key, target_key):
    setattr(settings, f"{prefix}{target_key}_move_x", getattr(settings, f"{prefix}{source_key}_move_x"))
    setattr(settings, f"{prefix}{target_key}_move_y", getattr(settings, f"{prefix}{source_key}_move_y"))
    setattr(settings, f"{prefix}{target_key}_move_z", getattr(settings, f"{prefix}{source_key}_move_z"))
    setattr(settings, f"{prefix}{target_key}_handle_length", getattr(settings, f"{prefix}{source_key}_handle_length"))
    setattr(settings, f"{prefix}{target_key}_handle_rotation", getattr(settings, f"{prefix}{source_key}_handle_rotation"))


def synchronize_mirrored_point_settings(settings):
    if settings.cycle_pair_negative_in_out:
        synchronize_mirrored_point_pair(settings, "", "negative_in", "negative_out")
    if settings.cycle_pair_positive_in_out:
        synchronize_mirrored_point_pair(settings, "", "positive_in", "positive_out")
    if settings.gather_pair_negative_in_out:
        synchronize_mirrored_point_pair(settings, "gather_", "negative_in", "negative_out")
    if settings.gather_pair_positive_in_out:
        synchronize_mirrored_point_pair(settings, "gather_", "positive_in", "positive_out")


def frill_settings_updated(self, context):
    global _is_auto_updating
    if _is_auto_updating:
        return

    _is_auto_updating = True
    try:
        synchronize_mirrored_point_settings(self)
    finally:
        _is_auto_updating = False

    self.frill_preset_unsaved = not current_settings_match_selected_preset(self, context)

    frill_obj = frill_object_from_context(context)
    if frill_obj is None:
        return

    set_frill_curve_modifier_visibility(frill_obj, self.curve_modifier_show_viewport)
    if frill_obj.get("frill_keep_curve_applied", False):
        _pending_frill_rebuilds.pop(frill_obj.name, None)
        return

    if not self.auto_update:
        return

    update_sec = max(0.0, min(MAX_UPDATE_DELAY_SECONDS, self.update_sec))
    if update_sec <= 0.000001:
        _pending_frill_rebuilds.pop(frill_obj.name, None)
        _is_auto_updating = True
        try:
            rebuild_frill_object(context, frill_obj, self)
        except ValueError:
            pass
        finally:
            _is_auto_updating = False
        return

    schedule_frill_rebuild(context, frill_obj, update_sec)


def frill_type_updated(self, context):
    global _is_auto_updating
    if self.frill_type == EDIT_MODE_IDENTIFIER:
        frill_settings_updated(self, context)
        return

    preset = read_selected_preset(self.frill_type)
    if preset is None:
        show_preset_error(context, "The selected preset file could not be loaded. Presets were reloaded.")
        reload_preset_cache()
        previous = _is_auto_updating
        _is_auto_updating = True
        try:
            ensure_valid_preset_selection(self)
        finally:
            _is_auto_updating = previous
        return

    previous = _is_auto_updating
    _is_auto_updating = True
    try:
        apply_preset_settings(self, preset["settings"])
        synchronize_mirrored_point_settings(self)
        self.frill_preset_unsaved = False
    finally:
        _is_auto_updating = previous

    frill_settings_updated(self, context)


@persistent
def frill_curve_depsgraph_update(scene, depsgraph):
    global _is_curve_mesh_updating
    if not _is_auto_updating:
        restore_settings_for_selected_frill(scene)
    if _is_curve_mesh_updating or _is_auto_updating:
        return

    mesh_objects = set()
    for update in depsgraph.updates:
        datablock = update.id
        if isinstance(datablock, bpy.types.Object):
            obj = datablock
            if obj.type == 'CURVE' and obj.get("frill_curve_role") in {'GATHER', 'TIP'} and "frill_mesh_object" in obj:
                mesh_objects.add(obj["frill_mesh_object"])
        elif isinstance(datablock, bpy.types.Curve) and datablock.get("frill_curve_role") in {'GATHER', 'TIP'} and "frill_mesh_object" in datablock:
            mesh_objects.add(datablock["frill_mesh_object"])

    if not mesh_objects:
        return

    _is_curve_mesh_updating = True
    try:
        for mesh_name in mesh_objects:
            frill_obj = bpy.data.objects.get(mesh_name)
            if frill_obj is None or frill_obj.type != 'MESH':
                continue
            gather_curve = gather_curve_from_frill(frill_obj)
            tip_curve = bpy.data.objects.get(frill_obj.get("frill_tip_curve", ""))
            if gather_curve is None or tip_curve is None:
                continue
            try:
                apply_strip_curve_mesh(frill_obj, gather_curve, tip_curve)
            except ValueError:
                continue
    finally:
        _is_curve_mesh_updating = False


def cycle_move_property(name, default_key):
    return FloatProperty(
        name=name,
        default=DEFAULT_SETTINGS[default_key],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Normalized point movement scaled by the current Height",
        update=frill_settings_updated,
    )


def cycle_handle_length_property(default_key):
    return FloatProperty(
        name="Handle Length",
        default=DEFAULT_SETTINGS[default_key],
        min=0.0,
        soft_max=4.0,
        precision=2,
        step=5,
        description="Multiplier for both left and right Bezier handles",
        update=frill_settings_updated,
    )


def cycle_handle_rotation_property(default_key):
    return FloatProperty(
        name="Handle Rotation",
        default=DEFAULT_SETTINGS[default_key],
        min=-360.0,
        max=360.0,
        precision=1,
        step=10,
        description="Rotates both Bezier handles around the local height axis",
        update=frill_settings_updated,
    )


def gather_move_property(name, default_key):
    return FloatProperty(
        name=name,
        default=DEFAULT_SETTINGS[default_key],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Normalized attach-shape point movement scaled by the current Height",
        update=frill_settings_updated,
    )


def gather_handle_length_property(default_key):
    return FloatProperty(
        name="Handle Length",
        default=DEFAULT_SETTINGS[default_key],
        min=0.0,
        soft_max=4.0,
        precision=2,
        step=5,
        description="Multiplier for both left and right attach-shape Bezier handles",
        update=frill_settings_updated,
    )


def gather_handle_rotation_property(default_key):
    return FloatProperty(
        name="Handle Rotation",
        default=DEFAULT_SETTINGS[default_key],
        min=-360.0,
        max=360.0,
        precision=1,
        step=10,
        description="Rotates both attach-shape Bezier handles around the local height axis",
        update=frill_settings_updated,
    )


class FRILLCREATE_PG_settings(PropertyGroup):
    width: FloatProperty(
        name="Height",
        default=DEFAULT_SETTINGS["width"],
        min=0.001,
        soft_max=100.0,
        precision=2,
        step=1,
        description="Distance from the attach edge to the frill tip",
        update=frill_settings_updated,
    )
    resolution_u: IntProperty(
        name="Curve Resolution",
        default=DEFAULT_SETTINGS["resolution_u"],
        min=1,
        max=2048,
        description="Mesh samples per Bezier curve segment along the frill length",
        update=frill_settings_updated,
    )
    resolution_v: IntProperty(
        name="Height Resolution",
        default=DEFAULT_SETTINGS["resolution_v"],
        min=1,
        max=64,
        description="Segments from the attach edge to the tip",
        update=frill_settings_updated,
    )
    frill_type: EnumProperty(
        name="Frill Type / Preset",
        items=preset_enum_items,
        update=frill_type_updated,
    )
    frill_preset_unsaved: BoolProperty(
        name="Unsaved Preset State",
        default=False,
        options={'HIDDEN'},
    )
    length_ratio: FloatProperty(
        name="Length (Ratio)",
        default=DEFAULT_SETTINGS["length_ratio"],
        min=0.01,
        soft_max=100.0,
        description="Generated frill length relative to the selected base line",
        update=frill_settings_updated,
    )
    direction_angle: FloatProperty(
        name="Angle 1",
        default=DEFAULT_SETTINGS["direction_angle"],
        min=-360.0,
        max=360.0,
        precision=2,
        step=1,
        description="Angle at the first angle point along the frill length",
        update=frill_settings_updated,
    )
    angle_direct_control: BoolProperty(
        name="Set Angle for each point",
        default=DEFAULT_SETTINGS["angle_direct_control"],
        description="Edit Source curve point tilt values directly instead of evenly distributed angle points",
        update=frill_settings_updated,
    )
    angle_point_count: IntProperty(
        name="Angle Point Count",
        default=DEFAULT_SETTINGS["angle_point_count"],
        min=1,
        max=ANGLE_POINT_MAX,
        description="Number of angle control points distributed along the frill length",
        update=frill_settings_updated,
    )
    edit_curve_point_count: IntProperty(
        name="Curve Point Count",
        default=DEFAULT_SETTINGS["edit_curve_point_count"],
        min=2,
        max=1024,
        description="Control point count used by EditMode; clamped to the selected edge vertex count",
        update=frill_settings_updated,
    )
    edit_linear_mesh_from_curve_points: BoolProperty(
        name="Linear Mesh from Curve Points",
        default=DEFAULT_SETTINGS["edit_linear_mesh_from_curve_points"],
        description="In EditMode, build the frill mesh directly from curve control points instead of preview-resolution samples",
        update=frill_settings_updated,
    )
    curve_modifier_show_viewport: BoolProperty(
        name="Show Curve Modifier",
        default=DEFAULT_SETTINGS["curve_modifier_show_viewport"],
        description="Toggle viewport visibility for the generated Curve modifier",
        update=frill_settings_updated,
    )
    curve_deform_axis: EnumProperty(
        name="Curve Deform Axis",
        items=CURVE_DEFORM_AXIS_ITEMS,
        default=DEFAULT_SETTINGS["curve_deform_axis"],
        description="Deformation axis used by the generated Curve modifier",
        update=frill_settings_updated,
    )
    reverse_source_curve: BoolProperty(
        name="Reverse Source Curve",
        default=DEFAULT_SETTINGS["reverse_source_curve"],
        description="Reverse the start and end points of the generated Source curve",
        update=frill_settings_updated,
    )
    source_curve_handle_type: EnumProperty(
        name="Source Curve Handle Type",
        items=SOURCE_CURVE_HANDLE_TYPE_ITEMS,
        default=DEFAULT_SETTINGS["source_curve_handle_type"],
        description="Bezier handle type used by the generated Source curve",
        update=frill_settings_updated,
    )
    turn_mesh_x: BoolProperty(
        name="Turn Mesh X",
        default=DEFAULT_SETTINGS["turn_mesh_x"],
        description="Mirror the local frill mesh so it extends along the negative X axis before curve deformation",
        update=frill_settings_updated,
    )
    gather_smoothing_enabled: BoolProperty(
        name="Add Blend Gather Mesh",
        default=DEFAULT_SETTINGS["gather_smoothing_enabled"],
        description="Insert an additional blend mesh between the fixed source edge and the gather line",
        update=frill_settings_updated,
    )
    gather_shape_offset: FloatProperty(
        name="Blend Gather Mesh Offset",
        default=DEFAULT_SETTINGS["gather_shape_offset"],
        min=0.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Offsets the blend gather line from the fixed source edge by Height units",
        update=frill_settings_updated,
    )
    tip_wave_count: IntProperty(
        name="Wave Count",
        default=DEFAULT_SETTINGS["tip_wave_count"],
        min=1,
        max=128,
        description="Number of bundles along the generated frill length",
        update=frill_settings_updated,
    )
    positive_mid_center: FloatProperty(
        name="Bulge Center",
        default=DEFAULT_SETTINGS["positive_mid_center"],
        min=0.0,
        max=1.0,
        precision=3,
        step=1,
        description="Height position used as the center of the front-facing positive middle shape",
        update=frill_settings_updated,
    )
    positive_mid_depth: FloatProperty(
        name="Mid Depth",
        default=DEFAULT_SETTINGS["positive_mid_depth"],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Depth offset applied through the middle of front-facing positive frill sections; scaled by Height",
        update=frill_settings_updated,
    )
    positive_mid_profile_power: FloatProperty(
        name="Power",
        default=DEFAULT_SETTINGS["positive_mid_profile_power"],
        min=0.0,
        max=1.0,
        precision=4,
        step=1,
        description="Controls positive mid-shape concentration; 0.0 is strongest and 1.0 is broadest",
        update=frill_settings_updated,
    )
    negative_mid_center: FloatProperty(
        name="Bulge Center",
        default=DEFAULT_SETTINGS["negative_mid_center"],
        min=0.0,
        max=1.0,
        precision=3,
        step=1,
        description="Height position used as the center of the front-facing negative middle shape",
        update=frill_settings_updated,
    )
    negative_mid_depth: FloatProperty(
        name="Mid Depth",
        default=DEFAULT_SETTINGS["negative_mid_depth"],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Depth offset applied through the middle of front-facing negative frill sections; scaled by Height",
        update=frill_settings_updated,
    )
    negative_mid_profile_power: FloatProperty(
        name="Power",
        default=DEFAULT_SETTINGS["negative_mid_profile_power"],
        min=0.0,
        max=1.0,
        precision=4,
        step=1,
        description="Controls negative mid-shape concentration; 0.0 is strongest and 1.0 is broadest",
        update=frill_settings_updated,
    )
    cycle_pair_positive_in_out: BoolProperty(
        name="Mirror Positive In/Out",
        default=DEFAULT_SETTINGS["cycle_pair_positive_in_out"],
        description="Use Positive In controls for Positive Out with mirrored length-axis movement",
        update=frill_settings_updated,
    )
    cycle_pair_negative_in_out: BoolProperty(
        name="Mirror Negative In/Out",
        default=DEFAULT_SETTINGS["cycle_pair_negative_in_out"],
        description="Use Negative In controls for Negative Out with mirrored length-axis movement",
        update=frill_settings_updated,
    )
    cycle_random_enabled: BoolProperty(
        name="Enable Random Mode",
        default=DEFAULT_SETTINGS["cycle_random_enabled"],
        description="Apply repeatable random offsets to tip control points; disabling returns to the original edited point values",
        update=frill_settings_updated,
    )
    cycle_random_seed: IntProperty(
        name="Random Seed",
        default=DEFAULT_SETTINGS["cycle_random_seed"],
        min=0,
        max=999999,
        description="Seed for repeatable tip point random offsets",
        update=frill_settings_updated,
    )
    cycle_random_move_offset: FloatProperty(
        name="Move Offset",
        default=DEFAULT_SETTINGS["cycle_random_move_offset"],
        min=0.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Maximum normalized Length, Height, and Depth offset for tip points",
        update=frill_settings_updated,
    )
    cycle_random_handle_offset: FloatProperty(
        name="Handle Offset",
        default=DEFAULT_SETTINGS["cycle_random_handle_offset"],
        min=0.0,
        soft_max=4.0,
        precision=3,
        step=1,
        description="Maximum random change applied to tip Bezier handle length multipliers",
        update=frill_settings_updated,
    )
    cycle_random_rotation_offset: FloatProperty(
        name="Rotation Offset",
        default=DEFAULT_SETTINGS["cycle_random_rotation_offset"],
        min=0.0,
        soft_max=180.0,
        precision=1,
        step=10,
        description="Maximum random Handle Rotation angle in degrees for tip points",
        update=frill_settings_updated,
    )
    cycle_all_depth_offset: FloatProperty(
        name="All Points Depth Offset",
        default=DEFAULT_SETTINGS["cycle_all_depth_offset"],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Move every editable tip control point together along the local Depth axis",
        update=frill_settings_updated,
    )
    gather_pair_positive_in_out: BoolProperty(
        name="Mirror Positive In/Out",
        default=DEFAULT_SETTINGS["gather_pair_positive_in_out"],
        description="Use gather Positive In controls for Positive Out with mirrored length-axis movement",
        update=frill_settings_updated,
    )
    gather_pair_negative_in_out: BoolProperty(
        name="Mirror Negative In/Out",
        default=DEFAULT_SETTINGS["gather_pair_negative_in_out"],
        description="Use gather Negative In controls for Negative Out with mirrored length-axis movement",
        update=frill_settings_updated,
    )
    gather_random_enabled: BoolProperty(
        name="Enable Random Mode",
        default=DEFAULT_SETTINGS["gather_random_enabled"],
        description="Apply repeatable random offsets to gather attach points; disabling returns to the original edited point values",
        update=frill_settings_updated,
    )
    gather_random_seed: IntProperty(
        name="Random Seed",
        default=DEFAULT_SETTINGS["gather_random_seed"],
        min=0,
        max=999999,
        description="Seed for repeatable gather attach point random offsets",
        update=frill_settings_updated,
    )
    gather_random_move_offset: FloatProperty(
        name="Move Offset",
        default=DEFAULT_SETTINGS["gather_random_move_offset"],
        min=0.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Maximum normalized Length, Height, and Depth offset for gather attach points",
        update=frill_settings_updated,
    )
    gather_random_handle_offset: FloatProperty(
        name="Handle Offset",
        default=DEFAULT_SETTINGS["gather_random_handle_offset"],
        min=0.0,
        soft_max=4.0,
        precision=3,
        step=1,
        description="Maximum random change applied to gather attach Bezier handle length multipliers",
        update=frill_settings_updated,
    )
    gather_random_rotation_offset: FloatProperty(
        name="Rotation Offset",
        default=DEFAULT_SETTINGS["gather_random_rotation_offset"],
        min=0.0,
        soft_max=180.0,
        precision=1,
        step=10,
        description="Maximum random Handle Rotation angle in degrees for gather attach points",
        update=frill_settings_updated,
    )
    gather_all_depth_offset: FloatProperty(
        name="All Points Depth Offset",
        default=DEFAULT_SETTINGS["gather_all_depth_offset"],
        soft_min=-2.0,
        soft_max=2.0,
        precision=3,
        step=1,
        description="Move every editable gather attach control point together along the local Depth axis",
        update=frill_settings_updated,
    )
    gather_negative_in_move_x: gather_move_property("Length Move", "gather_negative_in_move_x")
    gather_negative_in_move_y: gather_move_property("Height Move", "gather_negative_in_move_y")
    gather_negative_in_move_z: gather_move_property("Depth Move", "gather_negative_in_move_z")
    gather_negative_in_handle_length: gather_handle_length_property("gather_negative_in_handle_length")
    gather_negative_in_handle_rotation: gather_handle_rotation_property("gather_negative_in_handle_rotation")
    gather_negative_trough_move_x: gather_move_property("Length Move", "gather_negative_trough_move_x")
    gather_negative_trough_move_y: gather_move_property("Height Move", "gather_negative_trough_move_y")
    gather_negative_trough_move_z: gather_move_property("Depth Move", "gather_negative_trough_move_z")
    gather_negative_trough_handle_length: gather_handle_length_property("gather_negative_trough_handle_length")
    gather_negative_trough_handle_rotation: gather_handle_rotation_property("gather_negative_trough_handle_rotation")
    gather_negative_out_move_x: gather_move_property("Length Move", "gather_negative_out_move_x")
    gather_negative_out_move_y: gather_move_property("Height Move", "gather_negative_out_move_y")
    gather_negative_out_move_z: gather_move_property("Depth Move", "gather_negative_out_move_z")
    gather_negative_out_handle_length: gather_handle_length_property("gather_negative_out_handle_length")
    gather_negative_out_handle_rotation: gather_handle_rotation_property("gather_negative_out_handle_rotation")
    gather_positive_in_move_x: gather_move_property("Length Move", "gather_positive_in_move_x")
    gather_positive_in_move_y: gather_move_property("Height Move", "gather_positive_in_move_y")
    gather_positive_in_move_z: gather_move_property("Depth Move", "gather_positive_in_move_z")
    gather_positive_in_handle_length: gather_handle_length_property("gather_positive_in_handle_length")
    gather_positive_in_handle_rotation: gather_handle_rotation_property("gather_positive_in_handle_rotation")
    gather_positive_peak_move_x: gather_move_property("Length Move", "gather_positive_peak_move_x")
    gather_positive_peak_move_y: gather_move_property("Height Move", "gather_positive_peak_move_y")
    gather_positive_peak_move_z: gather_move_property("Depth Move", "gather_positive_peak_move_z")
    gather_positive_peak_handle_length: gather_handle_length_property("gather_positive_peak_handle_length")
    gather_positive_peak_handle_rotation: gather_handle_rotation_property("gather_positive_peak_handle_rotation")
    gather_positive_out_move_x: gather_move_property("Length Move", "gather_positive_out_move_x")
    gather_positive_out_move_y: gather_move_property("Height Move", "gather_positive_out_move_y")
    gather_positive_out_move_z: gather_move_property("Depth Move", "gather_positive_out_move_z")
    gather_positive_out_handle_length: gather_handle_length_property("gather_positive_out_handle_length")
    gather_positive_out_handle_rotation: gather_handle_rotation_property("gather_positive_out_handle_rotation")
    negative_in_move_x: cycle_move_property("Length Move", "negative_in_move_x")
    negative_in_move_y: cycle_move_property("Height Move", "negative_in_move_y")
    negative_in_move_z: cycle_move_property("Depth Move", "negative_in_move_z")
    negative_in_handle_length: cycle_handle_length_property("negative_in_handle_length")
    negative_in_handle_rotation: cycle_handle_rotation_property("negative_in_handle_rotation")
    negative_trough_move_x: cycle_move_property("Length Move", "negative_trough_move_x")
    negative_trough_move_y: cycle_move_property("Height Move", "negative_trough_move_y")
    negative_trough_move_z: cycle_move_property("Depth Move", "negative_trough_move_z")
    negative_trough_handle_length: cycle_handle_length_property("negative_trough_handle_length")
    negative_trough_handle_rotation: cycle_handle_rotation_property("negative_trough_handle_rotation")
    negative_out_move_x: cycle_move_property("Length Move", "negative_out_move_x")
    negative_out_move_y: cycle_move_property("Height Move", "negative_out_move_y")
    negative_out_move_z: cycle_move_property("Depth Move", "negative_out_move_z")
    negative_out_handle_length: cycle_handle_length_property("negative_out_handle_length")
    negative_out_handle_rotation: cycle_handle_rotation_property("negative_out_handle_rotation")
    positive_in_move_x: cycle_move_property("Length Move", "positive_in_move_x")
    positive_in_move_y: cycle_move_property("Height Move", "positive_in_move_y")
    positive_in_move_z: cycle_move_property("Depth Move", "positive_in_move_z")
    positive_in_handle_length: cycle_handle_length_property("positive_in_handle_length")
    positive_in_handle_rotation: cycle_handle_rotation_property("positive_in_handle_rotation")
    positive_peak_move_x: cycle_move_property("Length Move", "positive_peak_move_x")
    positive_peak_move_y: cycle_move_property("Height Move", "positive_peak_move_y")
    positive_peak_move_z: cycle_move_property("Depth Move", "positive_peak_move_z")
    positive_peak_handle_length: cycle_handle_length_property("positive_peak_handle_length")
    positive_peak_handle_rotation: cycle_handle_rotation_property("positive_peak_handle_rotation")
    positive_out_move_x: cycle_move_property("Length Move", "positive_out_move_x")
    positive_out_move_y: cycle_move_property("Height Move", "positive_out_move_y")
    positive_out_move_z: cycle_move_property("Depth Move", "positive_out_move_z")
    positive_out_handle_length: cycle_handle_length_property("positive_out_handle_length")
    positive_out_handle_rotation: cycle_handle_rotation_property("positive_out_handle_rotation")
    tip_lateral_amplitude: FloatProperty(
        name="Tip Lateral Amplitude",
        default=DEFAULT_SETTINGS["tip_lateral_amplitude"],
        min=-10.0,
        max=10.0,
        description="Lateral wave amplitude on the frill tip",
        update=frill_settings_updated,
    )
    tip_lateral_frequency: FloatProperty(
        name="Tip Lateral Frequency",
        default=DEFAULT_SETTINGS["tip_lateral_frequency"],
        min=0.01,
        max=128.0,
        description="Lateral wave frequency on the frill tip",
        update=frill_settings_updated,
    )
    tip_lateral_phase: FloatProperty(
        name="Tip Lateral Phase",
        default=DEFAULT_SETTINGS["tip_lateral_phase"],
        min=-360.0,
        max=360.0,
        precision=2,
        step=1,
        description="Lateral wave phase",
        update=frill_settings_updated,
    )
    tip_lateral_randomness: FloatProperty(
        name="Tip Lateral Randomness",
        default=DEFAULT_SETTINGS["tip_lateral_randomness"],
        min=0.0,
        max=1.0,
        description="Smooth random variation for lateral tip wave",
        update=frill_settings_updated,
    )
    seed: IntProperty(
        name="Seed",
        default=DEFAULT_SETTINGS["seed"],
        min=0,
        max=999999,
        description="Random seed",
        update=frill_settings_updated,
    )
    randomness: FloatProperty(
        name="Randomness",
        default=DEFAULT_SETTINGS["randomness"],
        min=0.0,
        max=1.0,
        description="Overall smooth randomness",
        update=frill_settings_updated,
    )
    smooth_shading: BoolProperty(
        name="Smooth Shading",
        default=DEFAULT_SETTINGS["smooth_shading"],
        description="Use smooth shading for generated frill mesh faces",
        update=frill_settings_updated,
    )
    auto_update: BoolProperty(
        name="Auto Update",
        default=DEFAULT_SETTINGS["auto_update"],
        description="Immediately rebuild the active generated frill when parameters change",
        update=frill_settings_updated,
    )
    update_sec: FloatProperty(
        name="Update Sec",
        default=DEFAULT_SETTINGS["update_sec"],
        min=0.0,
        max=MAX_UPDATE_DELAY_SECONDS,
        precision=2,
        step=1,
        description="Delay automatic rebuilds by this many seconds; 0.0 updates in realtime",
        update=frill_settings_updated,
    )
    ui_show_basic: BoolProperty(
        name="Basic Settings",
        default=True,
    )
    ui_show_lateral: BoolProperty(
        name="Lateral Shape",
        default=False,
    )
    ui_show_shape_adjustment: BoolProperty(
        name="Frill Shape Adjustment",
        default=False,
    )
    ui_show_randomness: BoolProperty(
        name="Randomness",
        default=False,
    )
    ui_show_mid_shape: BoolProperty(
        name="Mid Shape Control",
        default=False,
    )
    ui_show_cycle_points: BoolProperty(
        name="Hem Line Control",
        default=False,
    )
    ui_show_cycle_random: BoolProperty(
        name="Tip Random Mode",
        default=False,
    )
    ui_show_gather_attach: BoolProperty(
        name="Gather Line Control",
        default=False,
    )
    ui_show_gather_random: BoolProperty(
        name="Gather Random Mode",
        default=False,
    )
    ui_show_options: BoolProperty(
        name="Options",
        default=False,
    )
    ui_show_preset_options: BoolProperty(
        name="Preset Setting",
        default=False,
    )
    ui_show_angle_points: BoolProperty(
        name="Angle Points",
        default=False,
    )


def define_angle_point_properties():
    annotations = FRILLCREATE_PG_settings.__annotations__
    for index in range(2, ANGLE_POINT_MAX + 1):
        name = f"angle_point_{index:04d}"
        if name in annotations:
            continue
        annotations[name] = FloatProperty(
            name=f"Angle {index}",
            default=0.0,
            min=-360.0,
            max=360.0,
            precision=2,
            step=1,
            description=f"Angle at control point {index} along the frill length",
            update=frill_settings_updated,
        )


define_angle_point_properties()


class FRILLCREATE_AP_preferences(AddonPreferences):
    bl_idname = __package__ or __name__

    preset_directory: StringProperty(
        name="Preset Folder",
        default="",
        subtype='DIR_PATH',
        description="Folder used for loading and saving FrillCreate presets",
    )

    def draw(self, _context):
        self.layout.prop(self, "preset_directory", text="Preset Folder", translate=False)


class FRILLCREATE_OT_generate(Operator):
    bl_idname = "frillcreate.generate"
    bl_label = "Generate"
    bl_description = "Generate a frill mesh from the selected edge chain"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.frillcreate_settings
        global _is_auto_updating
        previous = _is_auto_updating
        _is_auto_updating = True
        try:
            apply_straight_preset_settings(settings)
        finally:
            _is_auto_updating = previous

        try:
            points, is_loop, source_obj, source_vertex_order, source_edges = selected_edge_world_points(context)
            columns = build_curve_control_frill_shape(points, is_loop, settings)
            create_curve_driven_frill_mesh(context, source_obj, points, columns, is_loop, settings, source_vertex_order, source_edges)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}

        self.report({'INFO'}, "Frill mesh generated.")
        return {'FINISHED'}


def remove_generated_curve(curve_obj):
    if curve_obj is None:
        return

    curve_data = curve_obj.data
    bpy.data.objects.remove(curve_obj, do_unlink=True)
    if curve_data is not None and curve_data.users == 0:
        bpy.data.curves.remove(curve_data)


def clear_frill_properties(obj):
    if obj is None:
        return

    for key in list(obj.keys()):
        if key.startswith("frill_"):
            del obj[key]
    data = getattr(obj, "data", None)
    if data is None:
        return
    for key in list(data.keys()):
        if key.startswith("frill_"):
            del data[key]


def finalize_frill_keep_curve(context, frill_obj):
    source_curve = bpy.data.objects.get(frill_obj.get("frill_source_curve", ""))
    gather_curve = gather_curve_from_frill(frill_obj)
    tip_curve = bpy.data.objects.get(frill_obj.get("frill_tip_curve", ""))
    curve_modifier = frill_obj.modifiers.get(CURVE_MODIFIER_NAME)
    if curve_modifier is None or curve_modifier.type != 'CURVE':
        raise ValueError("The active frill Curve modifier was not found.")
    if source_curve is None or source_curve.type != 'CURVE':
        raise ValueError("The active frill Source curve was not found.")

    curve_modifier.object = source_curve
    remove_generated_curve(gather_curve)
    remove_generated_curve(tip_curve)
    clear_frill_properties(frill_obj)
    clear_frill_properties(source_curve)

    context.view_layer.objects.active = frill_obj
    frill_obj.select_set(True)


class FRILLCREATE_OT_apply(Operator):
    bl_idname = "frillcreate.apply"
    bl_label = "Apply"
    bl_description = "Finalize the active frill while keeping its Curve modifier"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        frill_obj = frill_object_from_context(context)
        if frill_obj is None or frill_obj.type != 'MESH':
            self.report({'WARNING'}, "Select the active curve-driven frill mesh to apply.")
            return {'CANCELLED'}
        if not (frill_obj.get("frill_curve_driven", False) or frill_obj.get("frill_keep_curve_applied", False)):
            self.report({'WARNING'}, "Select the active curve-driven frill mesh to apply.")
            return {'CANCELLED'}

        if not frill_obj.get("frill_keep_curve_applied", False):
            settings = context.scene.frillcreate_settings
            _pending_frill_rebuilds.pop(frill_obj.name, None)
            try:
                rebuild_frill_object(context, frill_obj, settings)
            except ValueError as error:
                self.report({'WARNING'}, str(error))
                return {'CANCELLED'}

        try:
            finalize_frill_keep_curve(context, frill_obj)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}

        self.report({'INFO'}, "Active frill applied. Curve modifier was kept.")
        return {'FINISHED'}


class FRILLCREATE_OT_apply_keep_curve(Operator):
    bl_idname = "frillcreate.apply_keep_curve"
    bl_label = "Apply (Keep Curve)"
    bl_description = "Disable parameter editing while keeping generated curves for 3D View editing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        frill_obj = frill_object_from_context(context)
        if frill_obj is None or frill_obj.type != 'MESH' or not frill_obj.get("frill_curve_driven", False):
            self.report({'WARNING'}, "Select the active curve-driven frill mesh to apply with Keep Curve.")
            return {'CANCELLED'}
        if frill_obj.get("frill_keep_curve_applied", False):
            self.report({'INFO'}, "Keep Curve is already applied.")
            return {'FINISHED'}

        settings = context.scene.frillcreate_settings
        _pending_frill_rebuilds.pop(frill_obj.name, None)
        try:
            rebuild_frill_object(context, frill_obj, settings)
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}

        frill_obj["frill_keep_curve_applied"] = True
        frill_obj["frill_auto_update"] = False
        set_frill_curve_modifier_visibility(frill_obj, settings.curve_modifier_show_viewport)

        context.view_layer.objects.active = frill_obj
        frill_obj.select_set(True)
        self.report({'INFO'}, "Keep Curve applied. Parameter controls were disabled.")
        return {'FINISHED'}


class FRILLCREATE_OT_reset_settings(Operator):
    bl_idname = "frillcreate.reset_settings"
    bl_label = "Reset Defaults"
    bl_description = "Reset frill generator parameters to their defaults"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.frillcreate_settings
        global _is_auto_updating
        _is_auto_updating = True
        try:
            for name, value in DEFAULT_SETTINGS.items():
                setattr(settings, name, value)
            for index in range(2, ANGLE_POINT_MAX + 1):
                setattr(settings, f"angle_point_{index:04d}", 0.0)
            settings.frill_preset_unsaved = not current_settings_match_selected_preset(settings, context)
        finally:
            _is_auto_updating = False

        if settings.auto_update:
            frill_obj = frill_object_from_context(context)
            if frill_obj is not None:
                try:
                    rebuild_frill_object(context, frill_obj, settings)
                except ValueError as error:
                    self.report({'WARNING'}, str(error))
                    return {'CANCELLED'}

        self.report({'INFO'}, "Frill settings reset.")
        return {'FINISHED'}


class FRILLCREATE_OT_reload_presets(Operator):
    bl_idname = "frillcreate.reload_presets"
    bl_label = "Reload Presets"
    bl_description = "Reload preset files from the current Preset folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        count = reload_preset_cache()
        settings = context.scene.frillcreate_settings
        global _is_auto_updating
        previous = _is_auto_updating
        _is_auto_updating = True
        try:
            ensure_valid_preset_selection(settings)
        finally:
            _is_auto_updating = previous
        self.report({'INFO'}, f"Reloaded {count} preset file(s).")
        return {'FINISHED'}


class FRILLCREATE_OT_save_preset(Operator):
    bl_idname = "frillcreate.save_preset"
    bl_label = "Save Preset"
    bl_description = "Save the current non-option settings as a preset file"
    bl_options = {'REGISTER'}

    preset_name: StringProperty(
        name="Preset Name",
        default="New Preset",
        description="File name used for the saved preset",
    )
    overwrite_existing: BoolProperty(
        name="Overwrite existing preset",
        default=False,
        description="Allow saving over a preset file with the same name",
    )

    def invoke(self, context, _event):
        self.overwrite_existing = False
        return context.window_manager.invoke_props_dialog(self, width=360, confirm_text="Save")

    def draw(self, _context):
        self.layout.prop(self, "preset_name", text="Preset Name", translate=False)
        path = preset_path_for_name(self.preset_name)
        if path.exists():
            box = self.layout.box()
            box.label(text=f"{path.name} already exists.", icon='ERROR', translate=False)
            box.prop(self, "overwrite_existing", text="Overwrite existing preset", translate=False)

    def execute(self, context):
        settings = context.scene.frillcreate_settings
        path = preset_path_for_name(self.preset_name)
        if path.exists() and not self.overwrite_existing:
            self.report({'WARNING'}, f"{path.name} already exists. Enable overwrite to save.")
            return {'CANCELLED'}

        payload = {
            "is_builtin": False,
            "settings": {name: getattr(settings, name) for name in preset_setting_names(settings, context)},
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            self.report({'WARNING'}, f"Could not save preset: {error}")
            return {'CANCELLED'}

        reload_preset_cache()
        global _is_auto_updating
        previous = _is_auto_updating
        _is_auto_updating = True
        try:
            settings.frill_type = path.stem
            settings.frill_preset_unsaved = False
        finally:
            _is_auto_updating = previous
        frill_obj = active_frill_mesh_from_context(context)
        if frill_obj is not None and not frill_obj.get("frill_keep_curve_applied", False):
            frill_obj["frill_preset_clean"] = True
            frill_obj["frill_preset_identifier"] = path.stem
            frill_obj["frill_type"] = path.stem
        self.report({'INFO'}, f"Preset saved as {path.name}.")
        return {'FINISHED'}


class FRILLCREATE_OT_view_preset_folder(Operator):
    bl_idname = "frillcreate.view_preset_folder"
    bl_label = "View Preset Folder"
    bl_description = "Open the current Preset folder"
    bl_options = {'REGISTER'}

    def execute(self, _context):
        folder = ensure_preset_directory()
        try:
            bpy.ops.wm.path_open(filepath=str(folder))
        except RuntimeError as error:
            self.report({'WARNING'}, f"Could not open preset folder: {error}")
            return {'CANCELLED'}
        return {'FINISHED'}


class FRILLCREATE_OT_set_preset_folder(Operator):
    bl_idname = "frillcreate.set_preset_folder"
    bl_label = "Set Preset Folder"
    bl_description = "Choose the folder used for loading and saving presets"
    bl_options = {'REGISTER'}

    directory: StringProperty(
        name="Preset Folder",
        default="",
        subtype='DIR_PATH',
    )

    def invoke(self, context, _event):
        self.directory = str(preset_directory())
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            self.report({'WARNING'}, "Preset folder was not selected.")
            return {'CANCELLED'}

        folder = Path(bpy.path.abspath(self.directory)).expanduser()
        preferences = addon_preferences()
        if preferences is None:
            self.report({'WARNING'}, "Addon preferences could not be found.")
            return {'CANCELLED'}

        try:
            folder.mkdir(parents=True, exist_ok=True)
            created = ensure_default_presets_in_folder(folder)
        except OSError as error:
            self.report({'WARNING'}, f"Could not prepare preset folder: {error}")
            return {'CANCELLED'}

        preferences.preset_directory = str(folder)
        count = reload_preset_cache()
        settings = context.scene.frillcreate_settings
        global _is_auto_updating
        previous = _is_auto_updating
        _is_auto_updating = True
        try:
            ensure_valid_preset_selection(settings)
        finally:
            _is_auto_updating = previous

        if created:
            self.report({'INFO'}, f"Preset folder set. Created {len(created)} default preset(s), loaded {count} preset file(s).")
        else:
            self.report({'INFO'}, f"Preset folder set. Loaded {count} preset file(s).")
        return {'FINISHED'}


class FRILLCREATE_OT_open_readme(Operator):
    bl_idname = "frillcreate.open_readme"
    bl_label = "Open Readme"
    bl_description = "Open the local README file"
    bl_options = {'REGISTER'}

    def execute(self, _context):
        readme_path = Path(__file__).resolve().with_name("README.md")
        if not readme_path.exists():
            self.report({'WARNING'}, "README.md was not found.")
            return {'CANCELLED'}

        try:
            bpy.ops.wm.path_open(filepath=str(readme_path))
        except RuntimeError as error:
            self.report({'WARNING'}, f"Could not open README: {error}")
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_foldout(layout, settings, property_name, label):
    box = layout.box()
    row = box.row(align=True)
    row.alignment = 'LEFT'
    icon = 'TRIA_DOWN' if getattr(settings, property_name) else 'TRIA_RIGHT'
    row.prop(settings, property_name, text=label, icon=icon, emboss=False, translate=False)
    return box if getattr(settings, property_name) else None


def draw_prop(layout, settings, property_name, label):
    layout.prop(settings, property_name, text=label, translate=False)


def draw_frill_type_control(layout, settings):
    if settings.frill_preset_unsaved:
        layout.label(text="Frill Type / Preset: ***Unsaved***", translate=False)
        layout.prop(settings, "frill_type", text="Load Preset", translate=False)
        return

    draw_prop(layout, settings, "frill_type", "Frill Type / Preset")


def draw_update_sec(layout, settings):
    label = "Update Sec: Realtime" if settings.update_sec <= 0.000001 else "Update Sec"
    layout.prop(settings, "update_sec", text=label, slider=True, translate=False)


def angle_point_count_for_ui(context, settings):
    if not settings.angle_direct_control:
        return max(1, min(ANGLE_POINT_MAX, settings.angle_point_count))

    frill_obj = frill_object_from_context(context) if context is not None else None
    if frill_obj is not None:
        source_order = frill_obj.get("frill_source_vertex_order", [])
        if source_order:
            return max(1, min(ANGLE_POINT_MAX, len(source_order)))

    source_obj = context.object if context is not None else None
    if source_obj is not None and source_obj.type == 'MESH':
        try:
            points, _is_loop, _obj, _order, _edges = selected_edge_world_points(context)
            return max(1, min(ANGLE_POINT_MAX, len(points)))
        except ValueError:
            pass

    return max(1, min(ANGLE_POINT_MAX, settings.angle_point_count))


def draw_angle_point_controls(layout, settings, context=None):
    draw_prop(layout, settings, "angle_direct_control", "Set Angle for each point")
    if not settings.angle_direct_control:
        draw_prop(layout, settings, "angle_point_count", "Angle Point Count")

    count = angle_point_count_for_ui(context, settings)
    if count == 1 and not settings.angle_direct_control:
        draw_prop(layout, settings, "direction_angle", "Angle")
        return

    angle_box = draw_foldout(layout, settings, "ui_show_angle_points", "Angle Points")
    if not angle_box:
        return

    draw_prop(angle_box, settings, "direction_angle", "Angle 1")
    for index in range(2, count + 1):
        draw_prop(angle_box, settings, f"angle_point_{index:04d}", f"Angle {index}")


def draw_subsection(layout, label):
    box = layout.box()
    box.label(text=label, translate=False)
    return box


def draw_cycle_point_control(layout, settings, point_key, label, mirrored=False):
    box = layout.box()
    title_row = box.row(align=True)
    title_row.label(text=label, translate=False)
    if mirrored:
        title_row.label(text="Apply Mirror", translate=False)
        return

    controls = box.column(align=True)
    move_row = controls.row(align=True)
    move_row.prop(settings, f"{point_key}_move_x", text="Length", translate=False)
    move_row.prop(settings, f"{point_key}_move_y", text="Height", translate=False)
    move_row.prop(settings, f"{point_key}_move_z", text="Depth", translate=False)
    handle_row = controls.row(align=True)
    handle_row.prop(settings, f"{point_key}_handle_length", text="Handle Length", translate=False)
    handle_row.prop(settings, f"{point_key}_handle_rotation", text="Handle Rotation", translate=False)


def draw_gather_point_control(layout, settings, point_key, label, mirrored=False):
    box = layout.box()
    title_row = box.row(align=True)
    title_row.label(text=label, translate=False)
    if mirrored:
        title_row.label(text="Apply Mirror", translate=False)
        return

    controls = box.column(align=True)
    move_row = controls.row(align=True)
    move_row.prop(settings, f"gather_{point_key}_move_x", text="Length", translate=False)
    move_row.prop(settings, f"gather_{point_key}_move_y", text="Height", translate=False)
    move_row.prop(settings, f"gather_{point_key}_move_z", text="Depth", translate=False)
    handle_row = controls.row(align=True)
    handle_row.prop(settings, f"gather_{point_key}_handle_length", text="Handle Length", translate=False)
    handle_row.prop(settings, f"gather_{point_key}_handle_rotation", text="Handle Rotation", translate=False)


def draw_point_random_controls(layout, settings, prefix):
    draw_prop(layout, settings, f"{prefix}_random_enabled", "Enable Random Mode")
    draw_prop(layout, settings, f"{prefix}_random_seed", "Random Seed")
    draw_prop(layout, settings, f"{prefix}_random_move_offset", "Move Offset")
    draw_prop(layout, settings, f"{prefix}_random_handle_offset", "Handle Offset")
    draw_prop(layout, settings, f"{prefix}_random_rotation_offset", "Rotation Offset")


class FRILLCREATE_PT_panel(Panel):
    bl_label = "Frill Generator"
    bl_idname = "FRILLCREATE_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Frill Generator"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.frillcreate_settings

        generate_row = layout.row(align=True)
        generate_row.operator(FRILLCREATE_OT_generate.bl_idname, text="Generate", translate=False)
        apply_row = layout.row(align=True)
        apply_row.operator(FRILLCREATE_OT_apply.bl_idname, text="Apply", translate=False)
        apply_row.operator(FRILLCREATE_OT_apply_keep_curve.bl_idname, text="Apply (Keep Curve)", translate=False)

        modifier_box = layout.box()
        draw_prop(modifier_box, settings, "curve_modifier_show_viewport", "Show Curve Modifier")

        active_frill = frill_object_from_context(context)
        if active_frill is not None and active_frill.get("frill_keep_curve_applied", False):
            keep_curve_box = layout.box()
            keep_curve_box.label(text="Keep Curve has been applied.", translate=False)
            keep_curve_box.label(text="Parameter controls are disabled. Edit generated curves in 3D View.", translate=False)

            options_box = draw_foldout(layout, settings, "ui_show_options", "Options")
            if options_box:
                behavior_box = options_box.box()
                draw_prop(behavior_box, settings, "auto_update", "Auto Update")
                draw_update_sec(behavior_box, settings)
                draw_prop(behavior_box, settings, "smooth_shading", "Smooth Shading")

                preset_box = draw_foldout(options_box, settings, "ui_show_preset_options", "Preset Setting")
                if preset_box:
                    preset_box.operator(FRILLCREATE_OT_save_preset.bl_idname, text="Save Preset", translate=False)
                    preset_box.operator(FRILLCREATE_OT_reload_presets.bl_idname, text="Reload Presets", translate=False)
                    preset_box.operator(FRILLCREATE_OT_view_preset_folder.bl_idname, text="View Preset Folder", translate=False)
                    preset_box.operator(FRILLCREATE_OT_set_preset_folder.bl_idname, text="Set Preset Folder", translate=False)

                readme_box = options_box.box()
                readme_box.operator(FRILLCREATE_OT_open_readme.bl_idname, text="Open Readme", translate=False)

                reset_box = options_box.box()
                reset_box.operator(FRILLCREATE_OT_reset_settings.bl_idname, text="Reset Defaults", translate=False)
            return

        if is_edit_mode(settings):
            edit_box = layout.box()
            draw_frill_type_control(edit_box, settings)
            draw_prop(edit_box, settings, "edit_linear_mesh_from_curve_points", "Linear Mesh from Curve Points")
            draw_prop(edit_box, settings, "resolution_v", "Height Resolution")
            draw_prop(edit_box, settings, "resolution_u", "Curve Resolution")
            draw_prop(edit_box, settings, "edit_curve_point_count", "Curve Point Count")
            angle_box = draw_subsection(edit_box, "Angle")
            draw_angle_point_controls(angle_box, settings, context)
            edit_box.separator()
            draw_prop(edit_box, settings, "curve_deform_axis", "Curve Deform Axis")
            draw_prop(edit_box, settings, "reverse_source_curve", "Reverse Source Curve")
            draw_prop(edit_box, settings, "turn_mesh_x", "Turn Mesh X")

            options_box = draw_foldout(layout, settings, "ui_show_options", "Options")
            if options_box:
                behavior_box = options_box.box()
                draw_prop(behavior_box, settings, "auto_update", "Auto Update")
                draw_update_sec(behavior_box, settings)
                draw_prop(behavior_box, settings, "smooth_shading", "Smooth Shading")

                preset_box = draw_foldout(options_box, settings, "ui_show_preset_options", "Preset Setting")
                if preset_box:
                    preset_box.operator(FRILLCREATE_OT_save_preset.bl_idname, text="Save Preset", translate=False)
                    preset_box.operator(FRILLCREATE_OT_reload_presets.bl_idname, text="Reload Presets", translate=False)
                    preset_box.operator(FRILLCREATE_OT_view_preset_folder.bl_idname, text="View Preset Folder", translate=False)
                    preset_box.operator(FRILLCREATE_OT_set_preset_folder.bl_idname, text="Set Preset Folder", translate=False)

                readme_box = options_box.box()
                readme_box.operator(FRILLCREATE_OT_open_readme.bl_idname, text="Open Readme", translate=False)

                reset_box = options_box.box()
                reset_box.operator(FRILLCREATE_OT_reset_settings.bl_idname, text="Reset Defaults", translate=False)
            return

        basic_box = draw_foldout(layout, settings, "ui_show_basic", "Basic Settings")
        if basic_box:
            draw_frill_type_control(basic_box, settings)

            size_box = basic_box.box()
            draw_prop(size_box, settings, "width", "Height")
            draw_prop(size_box, settings, "length_ratio", "Length (Ratio)")
            draw_prop(size_box, settings, "resolution_v", "Height Resolution")
            draw_prop(size_box, settings, "resolution_u", "Curve Resolution")

            wave_box = basic_box.box()
            draw_prop(wave_box, settings, "tip_wave_count", "Wave Count")

            source_curve_box = basic_box.box()
            draw_prop(source_curve_box, settings, "source_curve_handle_type", "Source Curve Handle Type")

            direction_box = basic_box.box()
            draw_angle_point_controls(direction_box, settings, context)
            direction_box.separator()
            draw_prop(direction_box, settings, "curve_deform_axis", "Curve Deform Axis")
            draw_prop(direction_box, settings, "reverse_source_curve", "Reverse Source Curve")
            draw_prop(direction_box, settings, "turn_mesh_x", "Turn Mesh X")

            blend_gather_box = basic_box.box()
            draw_prop(blend_gather_box, settings, "gather_smoothing_enabled", "Add Blend Gather Mesh")
            draw_prop(blend_gather_box, settings, "gather_shape_offset", "Blend Gather Mesh Offset")

        shape_adjustment_box = draw_foldout(layout, settings, "ui_show_shape_adjustment", "Frill Shape Adjustment")
        if shape_adjustment_box:
            lateral_box = draw_foldout(shape_adjustment_box, settings, "ui_show_lateral", "Lateral Shape")
            if lateral_box:
                draw_prop(lateral_box, settings, "tip_lateral_amplitude", "Tip Lateral Amplitude")
                draw_prop(lateral_box, settings, "tip_lateral_frequency", "Tip Lateral Frequency")
                draw_prop(lateral_box, settings, "tip_lateral_phase", "Tip Lateral Phase")
                draw_prop(lateral_box, settings, "tip_lateral_randomness", "Tip Lateral Randomness")

            randomness_box = draw_foldout(shape_adjustment_box, settings, "ui_show_randomness", "Randomness")
            if randomness_box:
                draw_prop(randomness_box, settings, "randomness", "Randomness")
                draw_prop(randomness_box, settings, "seed", "Seed")

        mid_shape_box = draw_foldout(layout, settings, "ui_show_mid_shape", "Mid Shape Control")
        if mid_shape_box:
            positive_box = draw_subsection(mid_shape_box, "Positive")
            draw_prop(positive_box, settings, "positive_mid_center", "Bulge Center")
            draw_prop(positive_box, settings, "positive_mid_depth", "Mid Depth")
            draw_prop(positive_box, settings, "positive_mid_profile_power", "Power")

            negative_box = draw_subsection(mid_shape_box, "Negative")
            draw_prop(negative_box, settings, "negative_mid_center", "Bulge Center")
            draw_prop(negative_box, settings, "negative_mid_depth", "Mid Depth")
            draw_prop(negative_box, settings, "negative_mid_profile_power", "Power")

        cycle_box = draw_foldout(layout, settings, "ui_show_cycle_points", "Hem Line Control")
        if cycle_box:
            pair_box = draw_subsection(cycle_box, "Pair Options")
            pair_row = pair_box.row(align=True)
            draw_prop(pair_row, settings, "cycle_pair_positive_in_out", "Mirror (1) to (3)")
            draw_prop(pair_row, settings, "cycle_pair_negative_in_out", "Mirror (4) to (6)")
            draw_prop(cycle_box, settings, "cycle_all_depth_offset", "All Points Depth Offset")
            for display_index, (point_key, point_label) in enumerate(LINE_CONTROL_UI_ORDER, start=1):
                mirrored = (
                    (point_key == "negative_out" and settings.cycle_pair_negative_in_out)
                    or (point_key == "positive_out" and settings.cycle_pair_positive_in_out)
                )
                draw_cycle_point_control(cycle_box, settings, point_key, f"({display_index}) {point_label}", mirrored)
            cycle_random_box = draw_foldout(cycle_box, settings, "ui_show_cycle_random", "Random Mode")
            if cycle_random_box:
                draw_point_random_controls(cycle_random_box, settings, "cycle")

        gather_box = draw_foldout(layout, settings, "ui_show_gather_attach", "Gather Line Control")
        if gather_box:
            gather_pair_box = draw_subsection(gather_box, "Pair Options")
            gather_pair_row = gather_pair_box.row(align=True)
            draw_prop(gather_pair_row, settings, "gather_pair_positive_in_out", "Mirror (1) to (3)")
            draw_prop(gather_pair_row, settings, "gather_pair_negative_in_out", "Mirror (4) to (6)")
            draw_prop(gather_box, settings, "gather_all_depth_offset", "All Points Depth Offset")
            for display_index, (point_key, point_label) in enumerate(LINE_CONTROL_UI_ORDER, start=1):
                mirrored = (
                    (point_key == "negative_out" and settings.gather_pair_negative_in_out)
                    or (point_key == "positive_out" and settings.gather_pair_positive_in_out)
                )
                draw_gather_point_control(gather_box, settings, point_key, f"({display_index}) {point_label}", mirrored)
            gather_random_box = draw_foldout(gather_box, settings, "ui_show_gather_random", "Random Mode")
            if gather_random_box:
                draw_point_random_controls(gather_random_box, settings, "gather")

        options_box = draw_foldout(layout, settings, "ui_show_options", "Options")
        if options_box:
            behavior_box = options_box.box()
            draw_prop(behavior_box, settings, "auto_update", "Auto Update")
            draw_update_sec(behavior_box, settings)
            draw_prop(behavior_box, settings, "smooth_shading", "Smooth Shading")

            preset_box = draw_foldout(options_box, settings, "ui_show_preset_options", "Preset Setting")
            if preset_box:
                preset_box.operator(FRILLCREATE_OT_save_preset.bl_idname, text="Save Preset", translate=False)
                preset_box.operator(FRILLCREATE_OT_reload_presets.bl_idname, text="Reload Presets", translate=False)
                preset_box.operator(FRILLCREATE_OT_view_preset_folder.bl_idname, text="View Preset Folder", translate=False)
                preset_box.operator(FRILLCREATE_OT_set_preset_folder.bl_idname, text="Set Preset Folder", translate=False)

            readme_box = options_box.box()
            readme_box.operator(FRILLCREATE_OT_open_readme.bl_idname, text="Open Readme", translate=False)

            reset_box = options_box.box()
            reset_box.operator(FRILLCREATE_OT_reset_settings.bl_idname, text="Reset Defaults", translate=False)


classes = (
    FRILLCREATE_AP_preferences,
    FRILLCREATE_PG_settings,
    FRILLCREATE_OT_generate,
    FRILLCREATE_OT_apply,
    FRILLCREATE_OT_apply_keep_curve,
    FRILLCREATE_OT_reset_settings,
    FRILLCREATE_OT_reload_presets,
    FRILLCREATE_OT_save_preset,
    FRILLCREATE_OT_view_preset_folder,
    FRILLCREATE_OT_set_preset_folder,
    FRILLCREATE_OT_open_readme,
    FRILLCREATE_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frillcreate_settings = PointerProperty(type=FRILLCREATE_PG_settings)
    reload_preset_cache()
    schedule_scene_preset_defaults()
    if frill_curve_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(frill_curve_depsgraph_update)


def unregister():
    if bpy.app.timers.is_registered(initialize_scene_preset_defaults):
        bpy.app.timers.unregister(initialize_scene_preset_defaults)
    if bpy.app.timers.is_registered(process_pending_frill_rebuilds):
        bpy.app.timers.unregister(process_pending_frill_rebuilds)
    _pending_frill_rebuilds.clear()
    if frill_curve_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(frill_curve_depsgraph_update)
    del bpy.types.Scene.frillcreate_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _PRESET_CACHE.clear()
    _PRESET_ENUM_ITEMS.clear()


if __name__ == "__main__":
    register()

