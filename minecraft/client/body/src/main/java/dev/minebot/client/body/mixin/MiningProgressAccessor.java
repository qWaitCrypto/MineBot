package dev.minebot.client.body.mixin;

import net.minecraft.client.multiplayer.MultiPlayerGameMode;
import net.minecraft.core.BlockPos;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/** Read-only native mining progress; a stationary miner is still making progress. */
@Mixin(MultiPlayerGameMode.class)
public interface MiningProgressAccessor {
    @Accessor("isDestroying") boolean minebot$isDestroying();
    @Accessor("destroyBlockPos") BlockPos minebot$destroyBlockPos();
    @Accessor("destroyProgress") float minebot$destroyProgress();
}
