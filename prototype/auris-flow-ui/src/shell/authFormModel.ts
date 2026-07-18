import { AuthFormState } from "../shared/contracts/application";

export const defaultAuthForm: AuthFormState = {
  name: "",
  email: "demo.operator@auris.local",
  password: "auris-demo",
  tenant: "极光汽车",
  inviteCode: "",
  remember: true
};
