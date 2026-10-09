package dev.minebot.client.body.mixin;

import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.MultiPlayerGameMode;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.phys.BlockHitResult;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/** Remember what a client-initiated break/place touches; the server receipt decides whether it changed. */
@Mixin(MultiPlayerGameMode.class)
public abstract class WorldEditMixin {
    @Inject(method = "destroyBlock", at = @At("HEAD"))
    private void minebot$touchBreak(BlockPos pos, CallbackInfoReturnable<Boolean> ci) {
        MineBotClientBody.touchWorld(pos);
    }

    @Inject(method = "useItemOn", at = @At("HEAD"))
    private void minebot$touchPlace(LocalPlayer player, InteractionHand hand, BlockHitResult hit,
                                   CallbackInfoReturnable<InteractionResult> ci) {
        BlockPos pos = hit.getBlockPos();
        MineBotClientBody.touchWorld(pos);
        if (!Minecraft.getInstance().level.getBlockState(pos).canBeReplaced()) {
            MineBotClientBody.touchWorld(pos.relative(hit.getDirection()));
        }
    }
}
