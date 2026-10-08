package dev.minebot.client.body.mixin;

import com.mojang.blaze3d.platform.InputConstants;
import com.mojang.blaze3d.platform.Window;
import net.minecraft.client.Minecraft;
import net.minecraft.client.MouseHandler;
import org.lwjgl.glfw.GLFW;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Keep the desktop pointer free while the Agent owns in-world mouse actions. */
@Mixin(MouseHandler.class)
public abstract class ObserverMouseMixin {
    @Redirect(method = {"grabMouse", "releaseMouse"}, at = @At(value = "INVOKE",
        target = "Lcom/mojang/blaze3d/platform/InputConstants;grabOrReleaseMouse(Lcom/mojang/blaze3d/platform/Window;IDD)V"))
    private void minebot$desktopPointer(Window window, int mode, double x, double y) {
        if (Boolean.getBoolean("minebot.client.free_cursor")) {
            // Preserve Minecraft's logical grab: its block-breaking loop needs it.
            // Only the OS capture/warp changes, so native Body inputs still work.
            GLFW.glfwSetInputMode(window.handle(), GLFW.GLFW_CURSOR, GLFW.GLFW_CURSOR_NORMAL);
        } else {
            InputConstants.grabOrReleaseMouse(window, mode, x, y);
        }
    }

    @Inject(method = "turnPlayer", at = @At("HEAD"), cancellable = true)
    private void minebot$ignoreDesktopLook(double elapsed, CallbackInfo ci) {
        if (Boolean.getBoolean("minebot.client.free_cursor")) ci.cancel();
    }

    @Inject(method = {"onButton", "onScroll"}, at = @At("HEAD"), cancellable = true)
    private void minebot$ignoreWorldMouse(CallbackInfo ci) {
        if (Boolean.getBoolean("minebot.client.free_cursor") && Minecraft.getInstance().screen == null) {
            ci.cancel();
        }
    }
}
