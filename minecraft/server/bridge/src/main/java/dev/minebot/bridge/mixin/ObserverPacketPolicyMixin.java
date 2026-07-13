package dev.minebot.bridge.mixin;

import dev.minebot.bridge.observercontrol.ObserverInteractionPolicy;
import net.minecraft.network.protocol.game.ServerboundChatCommandPacket;
import net.minecraft.network.protocol.game.ServerboundChatCommandSignedPacket;
import net.minecraft.network.protocol.game.ServerboundChatPacket;
import net.minecraft.network.protocol.game.ServerboundContainerButtonClickPacket;
import net.minecraft.network.protocol.game.ServerboundContainerClickPacket;
import net.minecraft.network.protocol.game.ServerboundContainerSlotStateChangedPacket;
import net.minecraft.network.protocol.game.ServerboundInteractPacket;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.network.protocol.game.ServerboundPlayerActionPacket;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.network.protocol.game.ServerboundSetCreativeModeSlotPacket;
import net.minecraft.network.protocol.game.ServerboundUseItemOnPacket;
import net.minecraft.network.protocol.game.ServerboundUseItemPacket;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.ServerGamePacketListenerImpl;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ServerGamePacketListenerImpl.class)
public abstract class ObserverPacketPolicyMixin {
    @Shadow
    public ServerPlayer player;

    @Inject(method = "handleMovePlayer", at = @At("HEAD"), cancellable = true)
    private void minebot$denyMovement(ServerboundMovePlayerPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectMovement(player), callback);
    }

    @Inject(method = "handlePlayerAction", at = @At("HEAD"), cancellable = true)
    private void minebot$denyPlayerAction(ServerboundPlayerActionPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectBreak(player), callback);
    }

    @Inject(method = "handleUseItemOn", at = @At("HEAD"), cancellable = true)
    private void minebot$denyUseItemOn(ServerboundUseItemOnPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectUse(player), callback);
    }

    @Inject(method = "handleUseItem", at = @At("HEAD"), cancellable = true)
    private void minebot$denyUseItem(ServerboundUseItemPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectUse(player), callback);
    }

    @Inject(method = "handleInteract", at = @At("HEAD"), cancellable = true)
    private void minebot$denyInteract(ServerboundInteractPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectUse(player), callback);
    }

    @Inject(method = "handleSetCarriedItem", at = @At("HEAD"), cancellable = true)
    private void minebot$denySetCarriedItem(ServerboundSetCarriedItemPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectInventory(player), callback);
    }

    @Inject(method = "handleContainerClick", at = @At("HEAD"), cancellable = true)
    private void minebot$denyContainerClick(ServerboundContainerClickPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectInventory(player), callback);
    }

    @Inject(method = "handleContainerButtonClick", at = @At("HEAD"), cancellable = true)
    private void minebot$denyContainerButton(ServerboundContainerButtonClickPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectInventory(player), callback);
    }

    @Inject(method = "handleContainerSlotStateChanged", at = @At("HEAD"), cancellable = true)
    private void minebot$denyContainerSlotState(
        ServerboundContainerSlotStateChangedPacket packet,
        CallbackInfo callback
    ) {
        cancelIf(ObserverInteractionPolicy.rejectInventory(player), callback);
    }

    @Inject(method = "handleSetCreativeModeSlot", at = @At("HEAD"), cancellable = true)
    private void minebot$denyCreativeSlot(ServerboundSetCreativeModeSlotPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectInventory(player), callback);
    }

    @Inject(method = "handleChat", at = @At("HEAD"), cancellable = true)
    private void minebot$denyChat(ServerboundChatPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectChat(player), callback);
    }

    @Inject(method = "handleChatCommand", at = @At("HEAD"), cancellable = true)
    private void minebot$denyUnsignedCommand(ServerboundChatCommandPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectCommand(player), callback);
    }

    @Inject(method = "handleSignedChatCommand", at = @At("HEAD"), cancellable = true)
    private void minebot$denySignedCommand(ServerboundChatCommandSignedPacket packet, CallbackInfo callback) {
        cancelIf(ObserverInteractionPolicy.rejectCommand(player), callback);
    }

    private static void cancelIf(boolean denied, CallbackInfo callback) {
        if (denied) {
            callback.cancel();
        }
    }
}
