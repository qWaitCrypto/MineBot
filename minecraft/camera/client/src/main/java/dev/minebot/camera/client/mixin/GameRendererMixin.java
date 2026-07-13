package dev.minebot.camera.client.mixin;

import dev.minebot.camera.client.CameraController;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.Minecraft;
import net.minecraft.client.renderer.GameRenderer;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(GameRenderer.class)
public abstract class GameRendererMixin {
    @Shadow
    @Final
    private Minecraft minecraft;

    @Inject(method = "extract", at = @At("HEAD"))
    private void minebot$applyCameraDirective(DeltaTracker deltaTracker, boolean renderLevel, CallbackInfo callback) {
        CameraController.instance().applyFrame(minecraft, deltaTracker);
    }
}
