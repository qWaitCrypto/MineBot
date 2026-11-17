package dev.minebot.client.body.mixin;

import com.mojang.blaze3d.platform.InputConstants;
import com.mojang.blaze3d.platform.Window;
import net.minecraft.client.Minecraft;
import net.minecraft.client.MouseHandler;
import net.minecraft.client.input.MouseButtonInfo;
import org.lwjgl.glfw.GLFW;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** Keep the desktop pointer free; hold right mouse to look around. */
@Mixin(MouseHandler.class)
public abstract class ObserverMouseMixin {
    @Shadow private double accumulatedDX;
    @Shadow private double accumulatedDY;
    @Shadow private boolean ignoreFirstMove;
    @Unique private boolean minebot$looking;

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
    private void minebot$desktopLook(double elapsed, CallbackInfo ci) {
        if (!Boolean.getBoolean("minebot.client.free_cursor")) return;
        Minecraft minecraft = Minecraft.getInstance();
        if (minebot$looking && (minecraft.screen != null || !minecraft.isWindowActive()
            || GLFW.glfwGetMouseButton(minecraft.getWindow().handle(), GLFW.GLFW_MOUSE_BUTTON_RIGHT)
                != GLFW.GLFW_PRESS)) {
            minebot$setLooking(false);
        }
        if (!minebot$looking) {
            accumulatedDX = accumulatedDY = 0;
            ci.cancel();
        }
    }

    @Inject(method = "onButton", at = @At("HEAD"), cancellable = true)
    private void minebot$lookButton(long window, MouseButtonInfo button, int action, CallbackInfo ci) {
        if (!Boolean.getBoolean("minebot.client.free_cursor")) return;
        Minecraft minecraft = Minecraft.getInstance();
        if (window != minecraft.getWindow().handle()) return;
        if (button.button() == GLFW.GLFW_MOUSE_BUTTON_RIGHT && action == GLFW.GLFW_RELEASE
            && minebot$looking) minebot$setLooking(false);
        if (minecraft.screen == null) {
            if (button.button() == GLFW.GLFW_MOUSE_BUTTON_RIGHT && action == GLFW.GLFW_PRESS
                && minecraft.player != null && minecraft.isWindowActive()) {
                minecraft.mouseHandler.grabMouse();
                minebot$setLooking(true);
            }
            // Looking never places a block or uses an item.
            ci.cancel();
        }
    }

    @Inject(method = "releaseMouse", at = @At("HEAD"))
    private void minebot$releaseLook(CallbackInfo ci) {
        if (minebot$looking) minebot$setLooking(false);
    }

    @Inject(method = "onScroll", at = @At("HEAD"), cancellable = true)
    private void minebot$ignoreWorldScroll(CallbackInfo ci) {
        if (Boolean.getBoolean("minebot.client.free_cursor") && Minecraft.getInstance().screen == null) {
            ci.cancel();
        }
    }

    @Unique
    private void minebot$setLooking(boolean looking) {
        minebot$looking = looking;
        accumulatedDX = accumulatedDY = 0;
        ignoreFirstMove = true;
    }
}
