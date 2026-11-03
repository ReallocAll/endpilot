"""Session-scoped actor snapshot and runtime/unique ID indexes."""

from dataclasses import replace

from ._projection import ReadonlyMap


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
