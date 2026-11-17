# Ender dragon client facts (Minecraft 26.1.2)

The 26.1.2 client entity class has `EnderDragon.DATA_PHASE`, an integer in
synced entity data. The client reads that synchronized field and maps it with
`EnderDragonPhase.getById`; the entity JSON reports `phase_id` and `phase`.
`EnderDragon.getPhaseManager()` exists in the shared class, but its manager is
not a server observation channel. This client does not claim the server's
mutable phase instance as authoritative; it reports the synchronized phase
value the client received.

The client class also exposes `EnderDragon.head`, an `EnderDragonPart` with
ordinary entity coordinates. The client updates the part locally as the dragon
entity ticks. Its current coordinates are reported as `head_pos` (x, y, z);
they are a client observation, not an independently synchronized entity.

The `dragonFacts` client GameTest summons a dragon and asserts all three fields
appear in the client entity snapshot. It is written but has not been run in
this sandbox.
