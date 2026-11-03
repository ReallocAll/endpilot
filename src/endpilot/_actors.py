"""Session-scoped actor snapshot and runtime/unique ID indexes."""

from dataclasses import replace

from ._projection import ReadonlyMap
from .observations import ActorSnapshot
from .types import Vec3


def _vec(value):
    return Vec3(value.x, value.y, value.z)


class _ActorState:
    """Owns observed actors without retaining Session or event callbacks."""

    def __init__(self, p):
        self.p = p
        self.actors = {}
        self.unique_ids = {}

    def get(self, runtime_id):
        return self.actors.get(runtime_id)

    def runtime_id(self, unique_id):
        return self.unique_ids.get(unique_id)

    def has_unique_id(self, unique_id):
        return unique_id in self.unique_ids

    def values(self):
        return self.actors.values()

    def capture(self):
        return ReadonlyMap(self.actors)

    def clear(self):
        self.actors.clear()
        self.unique_ids.clear()

    def conflicts(self, actor):
        # Preserve removal order: conflicting unique ID first, then runtime ID.
        old = self.actors.get(actor.runtime_id)
        other = self.unique_ids.get(actor.unique_id)
        conflicts = []
        if other is not None and other != actor.runtime_id:
            conflicts.append(other)
        if old is not None and old.unique_id != actor.unique_id:
            conflicts.append(old.runtime_id)
        return conflicts

    def add(self, actor):
        self.actors[actor.runtime_id] = actor
        self.unique_ids[actor.unique_id] = actor.runtime_id

    def remove(self, runtime_id):
        actor = self.actors.pop(runtime_id, None)
        if actor is not None:
            self.unique_ids.pop(actor.unique_id, None)
        return actor

    def move(self, actor, position, pitch, yaw, head):
        actor = replace(actor, position=position, pitch=pitch, yaw=yaw, head_yaw=head)
        self.actors[actor.runtime_id] = actor
        return actor

    def snapshot(self, packet):
        """Construct an owned actor observation from the three spawn packet kinds."""
        if packet.packet_id == 13:
            return ActorSnapshot(
                packet.runtime_id,
                packet.entity_id,
                packet.actor_type,
                _vec(packet.pos),
                _vec(packet.velocity),
                packet.rot.x,
                packet.rot.y,
                packet.y_head_rotation,
                packet.y_body_rotation,
                packet.data.copy(),
                packet.synched_properties.copy(),
                packet.attributes.copy(),
                packet.links.copy(),
            )
        if packet.packet_id == 15:
            return ActorSnapshot(
                packet.runtime_id,
                packet.id,
                "minecraft:item",
                _vec(packet.pos),
                _vec(packet.velocity),
                metadata=packet.data.copy(),
                item=packet.item.copy(),
                from_fishing=packet.is_from_fishing,
            )
        if packet.packet_id == 12:
            return ActorSnapshot(
                packet.runtime_id,
                packet.abilities_data.target_player,
                "minecraft:player",
                _vec(packet.pos),
                velocity=_vec(packet.velocity),
                pitch=packet.rot.x,
                yaw=packet.rot.y,
                head_yaw=packet.y_head_rot,
                metadata=packet.unpack.copy(),
                properties=packet.synched_properties.copy(),
                links=packet.links.copy(),
            )
        raise ValueError("unexpected actor spawn packet")

    def motion(self, packet, protocol):
        """Project absolute/delta movement without publishing events or changing state."""
        move = packet.move_data
        actor = self.get(move.runtime_id)
        if actor is None:
            return None
        if packet.packet_id == 18:
            position = _vec(move.pos)
            pitch, yaw, head = (getattr(move, field) * 360 / 256 for field in ("rot_x", "rot_y", "rot_y_head"))
        else:
            position = actor.position
            coordinates = {}
            angles = [actor.pitch, actor.yaw, actor.head_yaw]
            for i, field in enumerate(
                ("new_position_x", "new_position_y", "new_position_z", "rot_x", "rot_y", "rot_y_head")
            ):
                value = getattr(move, field)
                present = value is not None if protocol == 2193 else bool(move.header & (1 << i))
                if present:
                    if i < 3:
                        coordinates[("x", "y", "z")[i]] = value
                    else:
                        angles[i - 3] = value * 360 / 256
            if coordinates:
                position = replace(position, **coordinates)
            pitch, yaw, head = angles
        return actor, position, pitch, yaw, head

    def update_attributes(self, packet):
        actor = self.get(packet.runtime_id)
        if actor is None or not packet.attribute_data:
            return None
        attributes = list(actor.attributes)
        for attribute in packet.attribute_data:
            value = self.p.SyncedAttribute(
                name=attribute.name,
                min_value=attribute.min_value,
                current_value=attribute.current_value,
                max_value=attribute.max_value,
            )
            for index, old in enumerate(attributes):
                if old.name == attribute.name:
                    attributes[index] = value
                    break
            else:
                attributes.append(value)
        actor = replace(actor, attributes=tuple(attributes))
        self.actors[packet.runtime_id] = actor
        return actor

    def update_data(self, packet):
        actor = self.get(packet.id)
        if actor is None:
            return None
        entries = {entry.id: entry for entry in actor.metadata.data} if actor.metadata is not None else {}
        entries.update((entry.id, entry.copy()) for entry in packet.packed_items.data)
        ints = (
            {entry.property_index: entry for entry in actor.properties.int_entries}
            if actor.properties is not None
            else {}
        )
        floats = (
            {entry.property_index: entry for entry in actor.properties.float_entries}
            if actor.properties is not None
            else {}
        )
        ints.update((entry.property_index, entry.copy()) for entry in packet.synched_properties.int_entries)
        floats.update((entry.property_index, entry.copy()) for entry in packet.synched_properties.float_entries)
        actor = replace(
            actor,
            metadata=self.p.SynchedActorData.CopyableDataList(data=entries.values()),
            properties=self.p.PropertySyncData(int_entries=ints.values(), float_entries=floats.values()),
        )
        self.actors[packet.id] = actor
        return actor

    def record_signal(self, packet):
        actor = self.get(packet.runtime_id)
        if actor is not None:
            self.actors[packet.runtime_id] = replace(actor, last_event=packet.copy())

    def matches(self, runtime_id, unique_id):
        actor = self.get(runtime_id)
        return actor is not None and actor.unique_id == unique_id
