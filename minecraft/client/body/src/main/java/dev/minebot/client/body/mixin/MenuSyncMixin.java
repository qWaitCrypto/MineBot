package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.game.ClientboundContainerSetContentPacket;
import net.minecraft.network.protocol.game.ClientboundContainerSetSlotPacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Correlate native menu opens and crafting outputs with server packets. */
@Mixin(ClientPacketListener.class)
public abstract class MenuSyncMixin {
    @Inject(method = "handleContainerContent", at = @At("TAIL"))
    private void minebot$content(ClientboundContainerSetContentPacket packet, CallbackInfo ci) {
        MineBotClientBody.observeMenuContent(packet.containerId(), packet.items());
    }

    @Inject(method = "handleContainerSetSlot", at = @At("TAIL"))
    private void minebot$slot(ClientboundContainerSetSlotPacket packet, CallbackInfo ci) {
        MineBotClientBody.observeMenuSlot(packet.getContainerId(), packet.getSlot(), packet.getItem());
    }
}
