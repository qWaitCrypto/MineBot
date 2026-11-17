package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.game.ClientboundSoundPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * A fully blocked hit deals no damage. In 26.1 the server signals it only by
 * playing the shield's block sound at the blocker (BlocksAttacks, sent to all
 * players including the blocker).
 */
@Mixin(ClientPacketListener.class)
public abstract class ShieldBlockMixin {
    @Inject(method = "handleSoundEvent", at = @At("HEAD"))
    private void minebot$shieldBlock(ClientboundSoundPacket packet, CallbackInfo ci) {
        // Called once on the network thread before being re-queued; count the render-thread call only.
        if (!Minecraft.getInstance().isSameThread()) return;
        MineBotClientBody.observeShieldBlock(packet.getSound().value().location().getPath(), packet.getX(), packet.getY(), packet.getZ());
    }
}
