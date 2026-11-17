package dev.minebot.client.body.gametest.mixin;

import dev.minebot.client.body.gametest.BodyTruthClientGameTest;

import net.minecraft.client.multiplayer.MultiPlayerGameMode;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.Player;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Observation only; packaged exclusively in the GameTest source set. */
@Mixin(MultiPlayerGameMode.class)
public abstract class BodyTruthAttackMixin {
    @Inject(method = "attack", at = @At("HEAD"))
    private void bodyTruth$attack(Player player, Entity target, CallbackInfo ci) {
        BodyTruthClientGameTest.observeAttack(player, target);
    }
}
