package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.game.ClientboundTakeItemEntityPacket;
import net.minecraft.network.protocol.game.ClientboundBlockUpdatePacket;
import net.minecraft.network.protocol.game.ClientboundSectionBlocksUpdatePacket;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Observe the server's recipient/count receipt before vanilla removes the drop. */
@Mixin(ClientPacketListener.class)
public abstract class ItemPickupMixin {
    @Inject(method = "handleTakeItemEntity", at = @At(value = "INVOKE",
        target = "Lnet/minecraft/client/multiplayer/ClientLevel;getEntity(I)Lnet/minecraft/world/entity/Entity;",
        ordinal = 0))
    private void minebot$pickupReceipt(ClientboundTakeItemEntityPacket packet, CallbackInfo ci) {
        MineBotClientBody.observePickup(packet.getItemId(), packet.getPlayerId(), packet.getAmount());
    }

    @Inject(method = "handleBlockUpdate", at = @At("TAIL"))
    private void minebot$blockReceipt(ClientboundBlockUpdatePacket packet, CallbackInfo ci) {
        MineBotClientBody.observeBlockUpdate(packet.getPos(), packet.getBlockState());
    }

    @Inject(method = "handleChunkBlocksUpdate", at = @At("TAIL"))
    private void minebot$sectionReceipt(ClientboundSectionBlocksUpdatePacket packet, CallbackInfo ci) {
        packet.runUpdates(MineBotClientBody::observeBlockUpdate);
    }
}
