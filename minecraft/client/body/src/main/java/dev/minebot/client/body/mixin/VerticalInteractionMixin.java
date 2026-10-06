package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.MultiPlayerGameMode;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.phys.BlockHitResult;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/** Bound upstream vertical break/place before any prediction or packet is sent. */
@Mixin(MultiPlayerGameMode.class)
public abstract class VerticalInteractionMixin {
    @Inject(method = {"startDestroyBlock", "continueDestroyBlock"}, at = @At("HEAD"), cancellable = true)
    private void minebot$verticalBreak(BlockPos pos, Direction direction, CallbackInfoReturnable<Boolean> ci) {
        if (!MineBotClientBody.allowVerticalMutation(pos)) ci.setReturnValue(false);
    }

    @Inject(method = "useItemOn", at = @At("HEAD"), cancellable = true)
    private void minebot$verticalPlace(LocalPlayer player, InteractionHand hand, BlockHitResult hit,
                                      CallbackInfoReturnable<InteractionResult> ci) {
        if (!(player.getItemInHand(hand).getItem() instanceof BlockItem)) return;
        BlockPos pos = hit.getBlockPos();
        if (!Minecraft.getInstance().level.getBlockState(pos).canBeReplaced()) pos = pos.relative(hit.getDirection());
        if (!MineBotClientBody.allowVerticalMutation(pos)) ci.setReturnValue(InteractionResult.FAIL);
    }

    @Inject(method = "useItem", at = @At("HEAD"), cancellable = true)
    private void minebot$crossbowShot(Player player, InteractionHand hand,
                                    CallbackInfoReturnable<InteractionResult> ci) {
        if (hand == InteractionHand.MAIN_HAND && !MineBotClientBody.observeCrossbowUse()) {
            ci.setReturnValue(InteractionResult.FAIL);
        }
    }
}
