package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.game.ClientboundDamageEventPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Preserve the server-observed damage source for the next Body fact sample. */
@Mixin(ClientPacketListener.class)
public abstract class DamageEventMixin {
    @Inject(method = "handleDamageEvent", at = @At("TAIL"))
    private void minebot$damage(ClientboundDamageEventPacket packet, CallbackInfo ci) {
        MineBotClientBody.observeDamage(packet);
    }
}
