package dev.minebot.client.body.gametest.mixin;

import dev.minebot.client.body.gametest.BodyTruthClientGameTest;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.network.protocol.game.ServerboundAttackPacket;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.ServerGamePacketListenerImpl;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Observe every attack packet, including a swing rejected before Player.attack. */
@Mixin(ServerGamePacketListenerImpl.class)
public abstract class BodyTruthDamageMixin {
    @Shadow public ServerPlayer player;
    @Unique private float bodyTruth$healthBefore;
    @Unique private Entity bodyTruth$target;

    @Inject(method = "handleAttack", at = @At("HEAD"))
    private void bodyTruth$beforeAttack(ServerboundAttackPacket packet, CallbackInfo ci) {
        // The network thread calls this method once before enqueueing it on
        // the server. Only the server-thread invocation touches the fixture.
        if (!player.level().getServer().isSameThread()) return;
        bodyTruth$target = BodyTruthClientGameTest.recordingServerAttack()
            ? player.level().getEntity(packet.entityId()) : null;
        if (bodyTruth$target instanceof LivingEntity living) bodyTruth$healthBefore = living.getHealth();
    }

    @Inject(method = "handleAttack", at = @At("RETURN"))
    private void bodyTruth$afterAttack(ServerboundAttackPacket packet, CallbackInfo ci) {
        if (player.level().getServer().isSameThread() && bodyTruth$target != null) {
            BodyTruthClientGameTest.observeServerAttack(bodyTruth$target, bodyTruth$healthBefore);
        }
    }
}
