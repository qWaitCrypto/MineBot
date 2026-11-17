package dev.minebot.client.body.mixin;

import baritone.api.event.events.PlayerUpdateEvent;
import baritone.api.event.events.type.EventState;
import baritone.api.utils.Rotation;
import dev.minebot.client.body.MineBotClientBody;
import net.minecraft.client.Minecraft;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Smooth the visible travel camera after Baritone sends its precise rotation. */
@Mixin(targets = "baritone.behavior.LookBehavior", remap = false)
public abstract class AttentionMixin {
    @Unique private boolean minebot$hasTarget;
    @Unique private boolean minebot$blockInteract;
    @Unique private float minebot$previousYaw;

    @Inject(method = "updateTarget", at = @At("HEAD"))
    private void minebot$target(Rotation rotation, boolean blockInteract, CallbackInfo ci) {
        minebot$hasTarget = true;
        minebot$blockInteract = blockInteract;
    }

    @Inject(method = "onPlayerUpdate", at = @At("HEAD"))
    private void minebot$beforeUpdate(PlayerUpdateEvent event, CallbackInfo ci) {
        if (event.getState() == EventState.PRE && Minecraft.getInstance().player != null) {
            minebot$previousYaw = Minecraft.getInstance().player.getYRot();
        }
    }

    @Inject(method = "onPlayerUpdate", at = @At("TAIL"))
    private void minebot$afterUpdate(PlayerUpdateEvent event, CallbackInfo ci) {
        if (event.getState() == EventState.POST) {
            if (minebot$hasTarget) MineBotClientBody.smoothTravelLook(minebot$previousYaw, minebot$blockInteract);
            minebot$hasTarget = false;
        }
    }
}
